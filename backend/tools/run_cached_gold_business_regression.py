from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import msvcrt
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import URLError
from urllib.request import Request, urlopen


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "test-results"
    / "gold-mail-regression"
    / "gold-20260812-batch01"
    / "manifest.json"
)
DEFAULT_API_PORT = 18010
DEFAULT_RELAY_PORT = 18765
AI_CACHE_SCHEMA = 1
OBJECT_CACHE_SCHEMA = 1
AI_GOLD_ITEM_FIELDS = {
    "sn",
    "board_code",
    "board_name",
    "fault_description",
    "failure_description",
}
RUNNER_CONTRACT = {
    "validate_manifest",
    "_require_sensitive_egress_approval",
    "run_suite",
    "cleanup",
    "report",
    "suite_root",
}


class CachedGoldError(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: bytes | str) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CachedGoldError("CACHE_DOCUMENT_INVALID", details={"path": str(path)})
    return value


def _optional_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    return datetime.fromisoformat(text) if text else None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_port(port: int, *, opened: bool, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port) is opened:
            return
        time.sleep(0.2)
    state = "OPEN" if opened else "CLOSED"
    raise CachedGoldError(f"PORT_DID_NOT_BECOME_{state}", details={"port": port})


def _safe_name(value: str | None) -> str:
    source = (value or "file").replace("\\", "_").replace("/", "_")
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in source)[:180] or "file"


class AiCompletionCache:
    def __init__(
        self,
        root: Path,
        original: Callable[..., Any],
        *,
        refresh: bool,
        max_live_calls: int,
        gold_fixtures: list[dict[str, Any]] | None = None,
    ) -> None:
        self.root = root
        self.original = original
        self.refresh = refresh
        self.max_live_calls = max_live_calls
        self.hits = 0
        self.misses = 0
        self.live_calls = 0
        self.refreshed = 0
        self.corrupt = 0
        self.gold_overlays = 0
        self.gold_fallback_hits = 0
        self.gold_fixture_replays = 0
        self.gold_fixtures = list(gold_fixtures or [])
        self.by_task: dict[str, dict[str, int]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _matching_gold_fixture(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        if len(self.gold_fixtures) == 1:
            return self.gold_fixtures[0]
        material = _canonical(messages).upper()
        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in self.gold_fixtures:
            sn_hits = sum(1 for sn in row["sns"] if sn and sn in material)
            field_markers = {
                str(value).strip().upper()
                for value in row["fields"].values()
                if value is not None and len(str(value).strip()) >= 5
            }
            field_hits = sum(1 for value in field_markers if value in material)
            score = (sn_hits * 100) + field_hits
            if score:
                ranked.append((score, row))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None
        return ranked[0][1]

    def _apply_gold_fixture(
        self,
        *,
        task_name: str,
        messages: list[dict[str, Any]],
        parsed: Any,
    ) -> Any:
        """Stabilize approved Gold extraction outputs for downstream-chain tests."""
        if task_name != "repair_field_extract" or not self.gold_fixtures:
            return parsed
        fixture = self._matching_gold_fixture(messages)
        if fixture is None or not hasattr(parsed, "extracted_fields"):
            return parsed
        result = parsed.model_copy(deep=True)
        result.intent_type = fixture["intent"]
        # A Gold replay must not leak fields from an unrelated cached email.
        result.extracted_fields = dict(fixture["fields"])
        replay_items: list[dict[str, Any]] = []
        for expected in fixture["items"]:
            sn = str(expected.get("sn") or "").strip().upper()
            replay_items.append(
                {
                    key: value
                    for key, value in {**dict(expected), "sn": sn}.items()
                    if key in AI_GOLD_ITEM_FIELDS
                }
            )
        result.extracted_items = replay_items
        if not fixture["missing_fields"]:
            result.missing_fields = {}
            result.conflict_fields = {}
        else:
            expected_missing = set(fixture["missing_fields"])
            result.missing_fields = {
                key: value
                for key, value in dict(result.missing_fields or {}).items()
                if key in expected_missing
            }
        result.confidence_score = max(float(result.confidence_score or 0), 0.95)
        result.evidence = {
            **dict(result.evidence or {}),
            "cached_gold_manifest_overlay": True,
        }
        self.gold_overlays += 1
        return result

    def _fingerprint(
        self,
        *,
        task: Any,
        messages: list[dict[str, Any]],
        response_model: Any,
        temperature: float | None,
    ) -> tuple[str, dict[str, Any]]:
        schema = response_model.model_json_schema()
        task_name = str(getattr(task, "value", task))
        material = {
            "schema_version": AI_CACHE_SCHEMA,
            "task": task_name,
            "messages": messages,
            "response_schema": schema,
            "temperature": temperature,
        }
        return _sha256(_canonical(material)), material

    def _task_counter(self, task: str) -> dict[str, int]:
        return self.by_task.setdefault(task, {"hits": 0, "misses": 0, "live_calls": 0, "refreshed": 0})

    async def invoke_structured(
        self,
        *,
        task: Any,
        messages: list[dict[str, Any]],
        response_model: Any,
        temperature: float | None = None,
    ) -> Any:
        from app.integrations.ai_provider import AiJsonCompletion, AiProviderError

        key, material = self._fingerprint(
            task=task,
            messages=messages,
            response_model=response_model,
            temperature=temperature,
        )
        task_name = str(material["task"])
        counters = self._task_counter(task_name)
        path = self.root / task_name / f"{key}.json"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if path.exists() and not self.refresh:
                try:
                    cached = _read_json(path)
                    if cached.get("schema_version") != AI_CACHE_SCHEMA or cached.get("fingerprint") != key:
                        raise ValueError("cache envelope mismatch")
                    parsed = response_model.model_validate(cached["parsed"])
                    parsed = self._apply_gold_fixture(
                        task_name=task_name,
                        messages=messages,
                        parsed=parsed,
                    )
                    self.hits += 1
                    counters["hits"] += 1
                    return AiJsonCompletion(
                        trace_id=f"cache-{key[:26]}",
                        request_payload={"cache_fingerprint": key, "cache_hit": True},
                        response_payload=cached.get("response_payload") or {"cache_hit": True},
                        output_text=str(cached.get("output_text") or ""),
                        parsed=parsed,
                        latency_ms=0,
                        task=task_name,
                        route_name=f"cache:{cached.get('route_name') or 'captured'}",
                        provider_name=f"cache:{cached.get('provider_name') or 'captured'}",
                        model_name=str(cached.get("model_name") or "cached"),
                        route_attempt=1,
                        fallback_used=False,
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self.corrupt += 1
                    quarantine = path.with_suffix(f".corrupt-{int(time.time())}.json")
                    path.replace(quarantine)

            fixture = self._matching_gold_fixture(messages)
            if task_name == "repair_field_extract" and fixture is not None and not self.refresh:
                expected_sns = set(fixture["sns"])
                for candidate in sorted((self.root / task_name).glob("*.json"), reverse=True):
                    try:
                        cached = _read_json(candidate)
                        parsed = response_model.model_validate(cached["parsed"])
                        cached_sns = {
                            str(item.get("sn") or "").strip().upper()
                            for item in (parsed.extracted_items or [])
                            if isinstance(item, dict)
                        }
                        if not expected_sns.issubset(cached_sns):
                            continue
                        parsed = self._apply_gold_fixture(
                            task_name=task_name,
                            messages=messages,
                            parsed=parsed,
                        )
                        self.hits += 1
                        self.gold_fallback_hits += 1
                        counters["hits"] += 1
                        return AiJsonCompletion(
                            trace_id=f"gold-cache-{candidate.stem[:21]}",
                            request_payload={"gold_cache_fallback": True},
                            response_payload=cached.get("response_payload") or {"cache_hit": True},
                            output_text=str(cached.get("output_text") or ""),
                            parsed=parsed,
                            latency_ms=0,
                            task=task_name,
                            route_name=f"gold-cache:{cached.get('route_name') or 'captured'}",
                            provider_name=f"gold-cache:{cached.get('provider_name') or 'captured'}",
                            model_name=str(cached.get("model_name") or "cached"),
                            route_attempt=1,
                            fallback_used=False,
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue

                # This runner verifies the downstream business chain, not live
                # extraction quality.  Replaying the approved Gold fields keeps
                # the run local and deterministic when a schema change invalidates
                # old AI fingerprints.  Material fields are deliberately excluded;
                # production must resolve them from SN master data.
                parsed = response_model.model_validate(
                    {
                        "intent_type": fixture["intent"],
                        "handling_level": "auto_repair",
                        "classification_reason_code": "CACHED_GOLD_FIXTURE_REPLAY",
                        "extracted_fields": fixture["fields"],
                        "extracted_items": fixture["items"],
                        "missing_fields": {
                            key: "Gold fixture expects customer supplementation"
                            for key in fixture["missing_fields"]
                        },
                        "confidence_score": 1,
                        "evidence": {"cached_gold_manifest_fixture": True},
                    }
                )
                parsed = self._apply_gold_fixture(
                    task_name=task_name,
                    messages=messages,
                    parsed=parsed,
                )
                self.hits += 1
                self.gold_fixture_replays += 1
                counters["hits"] += 1
                return AiJsonCompletion(
                    trace_id=f"gold-fixture-{key[:21]}",
                    request_payload={"gold_fixture_replay": True},
                    response_payload={"gold_fixture_replay": True},
                    output_text=_canonical(parsed.model_dump(mode="json")),
                    parsed=parsed,
                    latency_ms=0,
                    task=task_name,
                    route_name="gold-fixture:approved-manifest",
                    provider_name="gold-fixture:local",
                    model_name="approved-manifest",
                    route_attempt=1,
                    fallback_used=False,
                )

            self.misses += 1
            counters["misses"] += 1
            if self.live_calls >= self.max_live_calls:
                raise AiProviderError("AI_CACHE_LIVE_CALL_LIMIT_EXCEEDED")
            completion = await self.original(
                task=task,
                messages=messages,
                response_model=response_model,
                temperature=temperature,
            )
            self.live_calls += 1
            counters["live_calls"] += 1
            if self.refresh:
                self.refreshed += 1
                counters["refreshed"] += 1
            _write_json(
                path,
                {
                    "schema_version": AI_CACHE_SCHEMA,
                    "fingerprint": key,
                    "task": task_name,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "input_sha256": _sha256(_canonical(material["messages"])),
                    "response_schema_sha256": _sha256(_canonical(material["response_schema"])),
                    "parsed": completion.parsed.model_dump(mode="json"),
                    "response_payload": completion.response_payload,
                    "output_text": completion.output_text,
                    "provider_name": completion.provider_name,
                    "model_name": completion.model_name,
                    "route_name": completion.route_name,
                },
            )
            completion.parsed = self._apply_gold_fixture(
                task_name=task_name,
                messages=messages,
                parsed=completion.parsed,
            )
            return completion

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": AI_CACHE_SCHEMA,
            "hits": self.hits,
            "misses": self.misses,
            "live_calls": self.live_calls,
            "refreshed": self.refreshed,
            "corrupt_entries": self.corrupt,
            "gold_manifest_overlays": self.gold_overlays,
            "gold_fallback_hits": self.gold_fallback_hits,
            "gold_fixture_replays": self.gold_fixture_replays,
            "max_live_calls": self.max_live_calls,
            "by_task": self.by_task,
        }


class LocalObjectCache:
    BUCKET = "gold-business-local-cache"
    ENDPOINT = "local-cache://content-addressed"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploads = 0
        self.downloads = 0
        self.cache_hits = 0
        self.bytes_written = 0
        # Keep the object identity produced during this isolated run bound to
        # its local content.  Reply rendering can happen in a later worker
        # transaction; it must not depend on re-resolving a just-created test
        # object through AIRMA_test merely to read the cached bytes.
        self._paths_by_object_id: dict[int, Path] = {}

    def _content_path(self, digest: str) -> Path:
        return self.root / digest[:2] / f"{digest}.bin"

    async def upload_bytes_to_oss(
        self,
        session: Any,
        *,
        content: bytes,
        original_file_name: str | None,
        content_type: str | None,
        source_type: str,
        user_id: int | None = None,
    ) -> Any:
        from sqlalchemy import select
        from app.models import OssObject
        from app.services.storage import normalized_content_type

        digest = _sha256(content)
        path = self._content_path(digest)
        if path.exists():
            if _sha256(path.read_bytes()) != digest:
                raise CachedGoldError("LOCAL_OBJECT_CACHE_HASH_MISMATCH", details={"sha256": digest})
            self.cache_hits += 1
        else:
            _atomic_write(path, content)
            self.bytes_written += len(content)
        self.uploads += 1
        safe = _safe_name(original_file_name)
        object_key = f"{_safe_name(source_type)}/{digest[:2]}/{digest}-{safe}"
        existing = await session.scalar(
            select(OssObject).where(OssObject.bucket == self.BUCKET, OssObject.object_key == object_key)
        )
        if existing is not None:
            existing.upload_status = "success"
            self._paths_by_object_id[int(existing.id)] = path
            return existing
        row = OssObject(
            bucket=self.BUCKET,
            endpoint=self.ENDPOINT,
            object_key=object_key,
            original_file_name=original_file_name,
            safe_file_name=safe,
            content_type=normalized_content_type(original_file_name, content_type),
            file_size=len(content),
            sha256_hash=digest,
            etag=digest,
            source_type=source_type,
            upload_status="success",
            created_by_user_id=user_id,
        )
        session.add(row)
        await session.flush()
        self._paths_by_object_id[int(row.id)] = path
        return row

    async def download_oss_object_bytes(self, session: Any, *, oss_object_id: int) -> bytes:
        from app.models import OssObject
        from app.services.storage import StorageUploadError

        path = self._paths_by_object_id.get(int(oss_object_id))
        digest = path.stem if path is not None else ""
        if path is None:
            row = await session.get(OssObject, oss_object_id)
            if row is None:
                raise ValueError(f"OssObject with id {oss_object_id} not found")
            digest = str(row.sha256_hash or "")
            path = self._content_path(digest)
            self._paths_by_object_id[int(oss_object_id)] = path
        if not digest or not path.exists():
            raise StorageUploadError("LOCAL_OBJECT_CACHE_MISS")
        content = path.read_bytes()
        if _sha256(content) != digest:
            raise StorageUploadError("LOCAL_OBJECT_CACHE_HASH_MISMATCH")
        self.downloads += 1
        self.cache_hits += 1
        return content

    async def oss_object_exists(self, *, bucket: str, object_key: str, endpoint: str | None = None) -> bool:
        del endpoint
        if bucket != self.BUCKET:
            return False
        digest = object_key.rsplit("/", 1)[-1].split("-", 1)[0]
        return self._content_path(digest).exists()

    async def delete_oss_object(
        self,
        *,
        bucket: str,
        object_key: str,
        endpoint: str | None = None,
        object_version: str | None = None,
    ) -> Any:
        from app.services.storage import OssDeleteResult

        del endpoint, object_version
        # Database cleanup may delete the OssObject row. The content-addressed
        # fixture remains intentionally cached for the next business replay.
        return OssDeleteResult(bucket, object_key, True, already_missing=False)

    async def delete_oss_objects(self, objects: list[dict[str, Any]]) -> list[Any]:
        return [
            await self.delete_oss_object(
                bucket=str(item["bucket"]),
                object_key=str(item["object_key"]),
                endpoint=item.get("endpoint"),
                object_version=item.get("object_version"),
            )
            for item in objects
        ]

    async def generate_presigned_url_for_object(
        self, session: Any, *, oss_object_id: int, expires_seconds: int = 3600
    ) -> str:
        del session, expires_seconds
        return f"local-cache://object/{oss_object_id}"

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": OBJECT_CACHE_SCHEMA,
            "remote_oss_calls": 0,
            "uploads_intercepted": self.uploads,
            "downloads_served": self.downloads,
            "cache_hits": self.cache_hits,
            "bytes_written": self.bytes_written,
        }


def _sn_snapshot(row: Any) -> dict[str, Any]:
    fields = (
        "id", "sn", "ins_id", "customer_code", "customer_name", "material_code",
        "material_name", "asset_status", "warranty_start_date", "warranty_end_date",
        "source_file_name", "source_file_hash", "source_row_no", "source_system",
        "external_id", "source_updated_at", "raw_data",
    )
    return {field: getattr(row, field) for field in fields}


def _manifest_without_sn_rows(manifest: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(manifest)
    for message in stripped.get("messages") or []:
        (message.get("gold") or {})["temporary_sn_assets"] = []
    return stripped


def _install_temporary_master_overlay(gold: Any, batch: Any) -> tuple[Callable[..., Any], Callable[..., Any]]:
    from sqlalchemy import select
    from app.core.database import AsyncSessionLocal
    from app.models import ExternalSyncCheckpoint, SnAsset
    from app.services.common import utcnow
    from app.services.sap_sn_sync import CHECKPOINT_NAME

    original_apply = batch.apply_temporary_master_data
    original_cleanup = batch.cleanup_temporary_master_data

    async def apply_overlay(
        manifest: dict[str, Any],
        state_path: Path,
        *,
        allow_gold_e2e_snapshot_override: bool = False,
    ) -> dict[str, Any]:
        del allow_gold_e2e_snapshot_override
        state_path = Path(state_path)
        await original_apply(
            _manifest_without_sn_rows(manifest),
            state_path,
            allow_gold_e2e_snapshot_override=True,
        )
        state = _read_json(state_path)
        state.pop("cleanup", None)
        created = state.setdefault("temporary_master_data", {})
        created.setdefault("sn_asset_ids", [])
        snapshots = created.setdefault("overridden_sn_assets", [])
        rows, _, _ = batch._temporary_master_rows(manifest)
        batch_id = str(manifest["batch_id"])
        source_hash = _sha256(batch_id)
        async with AsyncSessionLocal() as session:
            checkpoint = await session.scalar(
                select(ExternalSyncCheckpoint).where(
                    ExternalSyncCheckpoint.sync_name == CHECKPOINT_NAME
                )
            )
            if "cached_gold_sn_checkpoint" not in state:
                state["cached_gold_sn_checkpoint"] = {
                    "existed": checkpoint is not None,
                    "cursor_value": checkpoint.cursor_value if checkpoint else None,
                    "last_full_sync_at": checkpoint.last_full_sync_at if checkpoint else None,
                    "last_success_at": checkpoint.last_success_at if checkpoint else None,
                    "last_status": checkpoint.last_status if checkpoint else None,
                    "last_error_code": checkpoint.last_error_code if checkpoint else None,
                    "statistics_json": checkpoint.statistics_json if checkpoint else None,
                }
            if checkpoint is None:
                checkpoint = ExternalSyncCheckpoint(sync_name=CHECKPOINT_NAME)
                session.add(checkpoint)
                await session.flush()
            now = utcnow()
            checkpoint.last_full_sync_at = now
            checkpoint.last_success_at = now
            checkpoint.last_status = "succeeded"
            checkpoint.last_error_code = None
            checkpoint.statistics_json = {
                "cached_gold_manifest_fixture": True,
                "batch_id": batch_id,
            }
            for row_no, fixture in enumerate(rows, 1):
                sn = str(fixture["sn"]).strip().upper()
                existing_rows = list(
                    (await session.execute(select(SnAsset).where(SnAsset.sn == sn))).scalars().all()
                )
                for existing in existing_rows:
                    if existing.id in created["sn_asset_ids"]:
                        continue
                    if not any(int(item.get("id") or 0) == existing.id for item in snapshots):
                        snapshots.append(_sn_snapshot(existing))
                    # Keep real rows completely outside the business lookup while
                    # the deterministic fixture is active. Merely changing the
                    # status still lets SN validation report a conflict.
                    existing.sn = f"__cached_gold_shadow_{existing.id}"
                    existing.asset_status = "gold_shadowed"
                    existing.source_file_name = batch_id
                    existing.raw_data = {
                        "cached_gold_run": True,
                        "batch_id": batch_id,
                        "shadowed_original": True,
                    }
                required = ("customer_code", "customer_name", "material_code")
                missing = [name for name in required if not str(fixture.get(name) or "").strip()]
                if missing:
                    raise CachedGoldError(
                        "TEMPORARY_SN_FIELDS_REQUIRED",
                        details={"sn_sha256": _sha256(sn), "fields": missing},
                    )
                ins_id = int(fixture["ins_id"])
                candidate = SnAsset(
                    ins_id=ins_id,
                    sn=sn,
                    customer_code=str(fixture["customer_code"]).strip(),
                    customer_name=str(fixture["customer_name"]).strip(),
                    material_code=str(fixture["material_code"]).strip(),
                    material_name=str(fixture.get("material_name") or "").strip() or None,
                    asset_status="valid",
                    warranty_start_date=batch._optional_date(fixture.get("warranty_start_date")),
                    warranty_end_date=batch._optional_date(fixture.get("warranty_end_date")),
                    source_file_name=batch_id,
                    source_file_hash=source_hash,
                    source_row_no=row_no,
                    source_system="e2e_cached_gold",
                    external_id=f"cached-gold:{batch_id}:{sn}",
                    raw_data={"batch_id": batch_id, "gold_confirmed": True, "cached_business_replay": True},
                )
                session.add(candidate)
                await session.flush()
                created["sn_asset_ids"].append(candidate.id)
            # State is durable before commit. A crash before commit is harmless;
            # a crash after commit is recoverable by the existing cleanup code.
            _write_json(state_path, state)
            await session.commit()
        return created

    async def cleanup_overlay(
        manifest_path: Path,
        *,
        state_path: Path | None = None,
        skip_manifest_validation: bool = False,
    ) -> dict[str, Any]:
        if state_path is not None and Path(state_path).exists():
            state = _read_json(Path(state_path))
            snapshots = (
                state.get("temporary_master_data", {}).get("overridden_sn_assets", [])
            )
            async with AsyncSessionLocal() as session:
                for snapshot in snapshots:
                    asset = await session.get(SnAsset, int(snapshot["id"]))
                    if asset is None:
                        continue
                    original_sn = str(snapshot["sn"])
                    if asset.sn == original_sn:
                        continue
                    marker = asset.raw_data if isinstance(asset.raw_data, dict) else {}
                    owns_shadow = (
                        str(asset.sn or "").startswith("__cached_gold_shadow_")
                        and asset.source_file_name == str(state.get("batch_id") or "")
                        and marker.get("cached_gold_run") is True
                        and marker.get("batch_id") == state.get("batch_id")
                        and marker.get("shadowed_original") is True
                    )
                    # A historical state file can survive after a later run has
                    # already restored or legitimately changed this row. Never
                    # overwrite a row unless the exact Gold shadow still owns it.
                    if not owns_shadow:
                        continue
                    asset.sn = original_sn
                checkpoint_state = state.get("cached_gold_sn_checkpoint")
                if isinstance(checkpoint_state, dict):
                    checkpoint = await session.scalar(
                        select(ExternalSyncCheckpoint).where(
                            ExternalSyncCheckpoint.sync_name == CHECKPOINT_NAME
                        )
                    )
                    marker = checkpoint.statistics_json if checkpoint else None
                    owned = (
                        isinstance(marker, dict)
                        and marker.get("cached_gold_manifest_fixture") is True
                        and marker.get("batch_id") == state.get("batch_id")
                    )
                    if checkpoint is not None and owned:
                        if checkpoint_state.get("existed"):
                            checkpoint.cursor_value = checkpoint_state.get("cursor_value")
                            checkpoint.last_full_sync_at = _optional_datetime(
                                checkpoint_state.get("last_full_sync_at")
                            )
                            checkpoint.last_success_at = _optional_datetime(
                                checkpoint_state.get("last_success_at")
                            )
                            checkpoint.last_status = str(
                                checkpoint_state.get("last_status") or "never_run"
                            )
                            checkpoint.last_error_code = checkpoint_state.get("last_error_code")
                            checkpoint.statistics_json = checkpoint_state.get("statistics_json")
                        else:
                            await session.delete(checkpoint)
                await session.commit()
        return await original_cleanup(
            manifest_path,
            state_path=state_path,
            skip_manifest_validation=skip_manifest_validation,
        )

    gold.apply_temporary_master_data = apply_overlay
    gold.cleanup_temporary_master_data = cleanup_overlay
    return apply_overlay, cleanup_overlay


def _install_ai_cache(cache: AiCompletionCache) -> None:
    from app.integrations import llm_gateway
    from app.services import ai, attachment_parser, mail_preclassification

    llm_gateway.invoke_structured = cache.invoke_structured
    ai.invoke_structured = cache.invoke_structured
    attachment_parser.invoke_structured = cache.invoke_structured
    mail_preclassification.invoke_structured = cache.invoke_structured


def _install_object_cache(cache: LocalObjectCache) -> None:
    from app.services import (
        attachment_parser,
        deletions,
        email_archival,
        email_preview,
        mail_processing,
        mail_reply_renderer,
        manual_review,
        replies,
        storage,
    )

    storage.upload_bytes_to_oss = cache.upload_bytes_to_oss
    storage.download_oss_object_bytes = cache.download_oss_object_bytes
    storage.oss_object_exists = cache.oss_object_exists
    storage.delete_oss_object = cache.delete_oss_object
    storage.delete_oss_objects = cache.delete_oss_objects
    storage.generate_presigned_url_for_object = cache.generate_presigned_url_for_object
    for module in (email_archival, manual_review, replies):
        if hasattr(module, "upload_bytes_to_oss"):
            module.upload_bytes_to_oss = cache.upload_bytes_to_oss
    for module in (
        attachment_parser,
        email_preview,
        mail_processing,
        mail_reply_renderer,
        manual_review,
        replies,
    ):
        if hasattr(module, "download_oss_object_bytes"):
            module.download_oss_object_bytes = cache.download_oss_object_bytes
    if hasattr(deletions, "delete_oss_object"):
        deletions.delete_oss_object = cache.delete_oss_object
    if hasattr(mail_processing, "delete_oss_object"):
        mail_processing.delete_oss_object = cache.delete_oss_object


def _install_local_fixture_snapshot_freshness() -> None:
    """Use the approved run-scoped SN fixtures as this run's fresh snapshot."""
    from app.services import ticket_safety
    from app.services.common import utcnow

    async def local_fixture_snapshot_freshness(_session: Any) -> dict[str, Any]:
        now = utcnow()
        return {
            "fresh": True,
            "status": "cached_gold_manifest_fixture",
            "last_success_at": now,
            "expires_at": now,
        }

    ticket_safety.sn_snapshot_freshness = local_fixture_snapshot_freshness


def _refresh_runtime_test_doubles(runtime: dict[str, Any], *, relay_port: int) -> None:
    """Re-assert run-local dependencies after the application lifespan starts.

    Startup refreshes persisted runtime configuration and may import worker
    modules after the initial patch pass.  Rebinding here keeps both request
    handling and background jobs on the same cached AI/object implementations
    and isolated relay instance.
    """
    settings = runtime["settings"]
    settings.RELAY_ADAPTER = "test_http"
    settings.RELAY_SQLSERVER_ENABLED = True
    settings.TEST_RELAY_BASE_URL = f"http://127.0.0.1:{relay_port}"
    _install_ai_cache(runtime["ai_cache"])
    _install_object_cache(runtime["object_cache"])
    _install_local_fixture_snapshot_freshness()
    # Bind the service-level factory as well as the Settings selector.  The
    # worker imports this symbol directly, so changing only RELAY_ADAPTER can
    # leave a previously resolved factory path active after lifespan startup.
    from app.integrations.sap_middleware import factory as middleware_factory
    from app.integrations.sap_middleware.test_http import TestHttpSapMiddlewareAdapter
    from app.services import job_dispatcher, jobs, relay_jobs, sap_rma

    _install_fast_job_retry()

    original_dispatch_job = job_dispatcher.dispatch_job

    async def diagnostic_execute_job_command(session: Any, job: Any) -> dict[str, Any]:
        try:
            return await original_dispatch_job(session, job)
        except Exception as exc:
            if str(getattr(job, "job_type", "")) != "email_parse":
                raise
            original = getattr(exc, "orig", None)
            _write_json(
                BACKEND_ROOT / "output" / f"cached-gold-job-exception-{job.id}.json",
                {
                    "exception": exc.__class__.__name__,
                    "original": original.__class__.__name__ if original is not None else None,
                    "args": list(getattr(original, "args", ()))[:2],
                    "statement": str(getattr(exc, "statement", "") or "")[:2000],
                    "frames": [
                        f"{frame.name}@{frame.lineno}"
                        for frame in traceback.extract_tb(exc.__traceback__)[-10:]
                    ],
                },
            )
            raise

    job_dispatcher.dispatch_job = diagnostic_execute_job_command

    def local_relay_factory() -> Any:
        runtime["relay_adapter_factory_calls"] = int(
            runtime.get("relay_adapter_factory_calls") or 0
        ) + 1
        return TestHttpSapMiddlewareAdapter()

    middleware_factory.create_sap_middleware_adapter = local_relay_factory
    sap_rma.create_sap_middleware_adapter = local_relay_factory
    # Functions retain their defining module globals even when imported by a
    # worker module.  Bind those dictionaries explicitly for an auditable,
    # run-local adapter selection.
    for function_name in (
        "submit_export_batch",
        "reconcile_uncertain_submission",
        "poll_export_batch",
        "poll_waiting_rma_results",
    ):
        function = getattr(sap_rma, function_name, None)
        if function is not None:
            function.__globals__["create_sap_middleware_adapter"] = local_relay_factory
    # relay_jobs imports the submit function by value at module import time.
    # Rebind that worker entry point to the patched function as well.
    relay_jobs.submit_export_batch = sap_rma.submit_export_batch


def _install_fast_job_retry() -> None:
    from app.services import jobs

    # A transient database disconnect must retry while the current case still
    # owns its temporary SN/board fixtures.  Production deliberately backs off
    # for minutes; this isolated regression polls every few seconds and cannot
    # allow a retry to outlive case cleanup and read restored real master data.
    local_job_retry_delay = lambda _job_type, _attempt_count: timedelta(seconds=1)
    jobs._job_retry_delay = local_job_retry_delay
    jobs.execute_claimed_job.__globals__["_job_retry_delay"] = local_job_retry_delay


def _assert_runner_contract(gold: Any, batch: Any) -> None:
    missing = sorted(name for name in RUNNER_CONTRACT if not hasattr(gold, name))
    missing.extend(
        name for name in ("apply_temporary_master_data", "cleanup_temporary_master_data", "_temporary_master_rows")
        if not hasattr(batch, name)
    )
    if missing:
        raise CachedGoldError("GOLD_RUNNER_CONTRACT_CHANGED", details={"missing": sorted(set(missing))})


def _install_cross_loop_database(app_main: Any, gold: Any, batch: Any) -> None:
    """Use unpooled connections because API and orchestrator own different event loops."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.config import settings
    from app.core import database
    from app import mail_worker, seed
    from app.services import mail_test_preflight

    original_engine = database.engine
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, pool_pre_ping=False)
    for event_name, listener_name in (
        ("before_cursor_execute", "_before_cursor_execute"),
        ("after_cursor_execute", "_after_cursor_execute"),
        ("handle_error", "_handle_database_error"),
    ):
        listener = getattr(database, listener_name, None)
        if listener is not None:
            event.listen(engine.sync_engine, event_name, listener)

    def _capture_unknown_column(context: Any) -> None:
        original = getattr(context, "original_exception", None)
        message = str(original)
        if "1054" not in message and "Unknown column" not in message:
            return
        match = re.search(r"Unknown column '([^']+)'", message)
        _write_json(
            BACKEND_ROOT / "output" / "cached-gold-unknown-column.json",
            {
                "error": "UNKNOWN_COLUMN",
                "column": match.group(1) if match else None,
                "detail": message[:300],
            },
        )
    event.listen(engine.sync_engine, "handle_error", _capture_unknown_column)
    event.listen(original_engine.sync_engine, "handle_error", _capture_unknown_column)
    event.listen(Engine, "handle_error", _capture_unknown_column)

    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    database.engine = engine
    database.AsyncSessionLocal = sessions
    for module in (app_main, gold):
        if hasattr(module, "engine"):
            module.engine = engine
    for module in (app_main, gold, batch, mail_worker, seed, mail_test_preflight):
        if hasattr(module, "AsyncSessionLocal"):
            module.AsyncSessionLocal = sessions


def _configure_environment(api_port: int, relay_port: int = DEFAULT_RELAY_PORT) -> None:
    os.environ["E2E_BASE_URL"] = f"http://127.0.0.1:{api_port}"
    os.environ["RELAY_ADAPTER"] = "test_http"
    os.environ["RELAY_SQLSERVER_ENABLED"] = "true"
    os.environ["TEST_RELAY_BASE_URL"] = f"http://127.0.0.1:{relay_port}"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


@contextmanager
def _managed_process(
    command: list[str],
    *,
    ready_port: int,
    require_new: bool = False,
    log_path: Path | None = None,
) -> Iterator[None]:
    process: subprocess.Popen[str] | None = None
    log_stream: Any = None
    if _port_open(ready_port) and require_new:
        raise CachedGoldError("ISOLATED_PROCESS_PORT_ALREADY_IN_USE", details={"port": ready_port})
    if not _port_open(ready_port):
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_stream = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=BACKEND_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_stream or subprocess.DEVNULL,
            stderr=log_stream or subprocess.DEVNULL,
            text=True,
        )
        _wait_port(ready_port, opened=True, timeout=35)
    try:
        yield
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            _wait_port(ready_port, opened=False, timeout=15)
        if log_stream is not None:
            log_stream.close()


@contextmanager
def _isolated_backend(app: Any, *, port: int) -> Iterator[None]:
    import uvicorn

    if _port_open(port):
        raise CachedGoldError("ISOLATED_API_PORT_ALREADY_IN_USE", details={"port": port})
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="cached-gold-api", daemon=True)
    thread.start()
    _wait_port(port, opened=True, timeout=45)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        if thread.is_alive():
            raise CachedGoldError("ISOLATED_API_DID_NOT_STOP")


@contextmanager
def _manifest_run_lock(manifest: Path) -> Iterator[None]:
    """Share the original suite lock so cached and ordinary Gold runs cannot overlap."""
    lock_path = manifest.parent / ".real-mail-run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    locked = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError as exc:
            raise CachedGoldError("REAL_MAIL_RUN_ALREADY_ACTIVE") from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def _relay_health(port: int, token: str) -> bool:
    try:
        request = Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urlopen(request, timeout=2) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _wait_relay_health(port: int, token: str, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _relay_health(port, token):
            return
        time.sleep(0.2)
    raise CachedGoldError("TEST_RELAY_HEALTH_FAILED", details={"port": port})


def _latest_result(root: Path, suite_id: str) -> Path | None:
    paths = sorted((root / suite_id / "runs").glob("*/result.json"))
    return paths[-1] if paths else None


def _runtime(args: argparse.Namespace) -> dict[str, Any]:
    if hasattr(args, "wait_timeout_seconds"):
        os.environ["E2E_WAIT_TIMEOUT_SECONDS"] = str(args.wait_timeout_seconds)
    _configure_environment(args.api_port, getattr(args, "relay_port", DEFAULT_RELAY_PORT))
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))

    from app.config import settings
    from app.integrations import llm_gateway
    from app import main as app_main
    from tools import run_gold_mail_regression as gold
    from tools import run_rmatest_batch_e2e as batch

    _assert_runner_contract(gold, batch)
    _install_fast_job_retry()
    # The production poll interval is intentionally several minutes. This
    # isolated test relay resolves immediately, so poll frequently enough that
    # a full RMA + SMTP assertion does not spend minutes idling.
    settings.RELAY_SQLSERVER_RMA_POLL_INTERVAL_SECONDS = 5
    if args.command == "run":
        wait_timeout = int(args.wait_timeout_seconds)
        gold._wait_for_parse_terminal.__kwdefaults__["timeout_seconds"] = wait_timeout
        gold._wait_for_case.__defaults__ = (wait_timeout,)
        gold._wait_for_case_outbound.__kwdefaults__["timeout_seconds"] = min(90, wait_timeout)
    _install_cross_loop_database(app_main, gold, batch)
    manifest = Path(args.manifest).resolve()
    validation = gold.validate_manifest(manifest, require_approval=True)
    if args.command == "run":
        gold._require_sensitive_egress_approval(manifest, json.loads(manifest.read_text(encoding="utf-8")))
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    suite_id = str(manifest_data["suite_id"])
    gold_fixtures = []
    selected_message_ids = set(getattr(args, "message_id", None) or [])
    fixture_messages = [
        message
        for message in manifest_data.get("messages") or []
        if not selected_message_ids or str(message.get("message_id") or "") in selected_message_ids
    ]
    for message in fixture_messages:
        gold_data = dict(message.get("gold") or {})
        material_items = [dict(item) for item in gold_data.get("expected_items") or []]
        board_items_by_sn = {
            str(item.get("sn") or "").strip().upper(): dict(item)
            for item in gold_data.get("expected_board_items") or []
        }
        items = [
            {
                key: value
                for key, value in {
                    "sn": material_item.get("sn"),
                    **board_items_by_sn.get(
                        str(material_item.get("sn") or "").strip().upper(), {}
                    ),
                }.items()
                if key in AI_GOLD_ITEM_FIELDS
            }
            for material_item in material_items
        ]
        gold_fixtures.append(
            {
                "intent": str(gold_data.get("expected_intent") or "unknown"),
                "fields": {
                    **dict(gold_data.get("expected_fields") or {}),
                    **dict(gold_data.get("expected_final_fields") or {}),
                },
                "items": items,
                "sns": [str(item.get("sn") or "").strip().upper() for item in items],
                "missing_fields": list(gold_data.get("missing_fields") or []),
            }
        )
    replay_root = PROJECT_ROOT / "test-results" / "cached-gold-business-regression"
    cache_root = replay_root / suite_id / "cache-v1"
    ai_cache = AiCompletionCache(
        cache_root / "ai",
        llm_gateway.invoke_structured,
        refresh=bool(getattr(args, "refresh_ai_cache", False)),
        max_live_calls=int(getattr(args, "max_live_ai_calls", 20)),
        gold_fixtures=gold_fixtures,
    )
    object_cache = LocalObjectCache(cache_root / "objects")
    _install_ai_cache(ai_cache)
    _install_object_cache(object_cache)
    _install_local_fixture_snapshot_freshness()
    _install_temporary_master_overlay(gold, batch)
    gold.EVIDENCE_ROOT = replay_root
    gold.ARCHIVE_DOC = replay_root / suite_id / "archive.md"
    gold._require_classification_gate = lambda _path, _manifest: {
        "status": "cached_business_replay",
        "runner": Path(__file__).name,
        "ai_cache_schema": AI_CACHE_SCHEMA,
    }

    async def no_scheduled_mailbox_scan() -> None:
        return None

    app_main._scheduled_imap_fetch = no_scheduled_mailbox_scan
    return {
        "settings": settings,
        "app_main": app_main,
        "gold": gold,
        "batch": batch,
        "manifest": manifest,
        "suite_id": suite_id,
        "replay_root": replay_root,
        "cache_root": cache_root,
        "ai_cache": ai_cache,
        "object_cache": object_cache,
        "validation": validation,
    }


def _augment_result(runtime: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    resource_usage = {
        "mode": "real_imap_smtp_cached_ai_local_oss",
        "ai": runtime["ai_cache"].summary(),
        "objects": runtime["object_cache"].summary(),
    }
    result["resource_usage"] = resource_usage
    result["test_relay_adapter_factory_calls"] = int(
        runtime.get("relay_adapter_factory_calls") or 0
    )
    latest = _latest_result(runtime["replay_root"], runtime["suite_id"])
    if latest is not None:
        persisted = _read_json(latest)
        persisted["resource_usage"] = resource_usage
        persisted["test_relay_adapter_factory_calls"] = result[
            "test_relay_adapter_factory_calls"
        ]
        _write_json(latest, persisted)
    return result


def _doctor(runtime: dict[str, Any], *, live: bool) -> dict[str, Any]:
    result = runtime["gold"].doctor(live=live)
    checks = list(result.get("checks") or [])
    checks.append(
        {
            "name": "local_cache",
            "passed": runtime["cache_root"].parent.exists() or runtime["cache_root"].parent.parent.exists(),
            "code": "OK",
            "detail": {"remote_oss_required": False},
        }
    )
    result["checks"] = checks
    result["mode"] = "real_imap_smtp_cached_ai_local_oss"
    return result


def _run(args: argparse.Namespace) -> dict[str, Any]:
    runtime = _runtime(args)
    settings = runtime["settings"]
    token = str(settings.TEST_RELAY_TOKEN or "")
    if len(token) < 24:
        raise CachedGoldError("TEST_RELAY_TOKEN_REQUIRED")
    relay_runtime_root = runtime["replay_root"] / runtime["suite_id"] / "relay-runs"
    relay_instance = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{os.getpid()}"
    relay_db = relay_runtime_root / f"{relay_instance}.sqlite3"
    relay_log = relay_runtime_root / f"{relay_instance}.log"
    tunnel_command = [sys.executable, "-m", "tools.run_mysql_ssh_tunnel"]
    relay_command = [
        sys.executable,
        "-m",
        "tools.test_relay_server",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.relay_port),
        "--database",
        str(relay_db),
        "--token",
        token,
    ]
    with _manifest_run_lock(runtime["manifest"]), ExitStack() as stack:
        stack.enter_context(_managed_process(tunnel_command, ready_port=13307))
        stack.enter_context(
            _managed_process(
                relay_command,
                ready_port=args.relay_port,
                require_new=True,
                log_path=relay_log,
            )
        )
        _wait_relay_health(args.relay_port, token)
        cleanup_preview = runtime["gold"].cleanup(
            runtime["manifest"], apply=False, expected_hash=None
        )
        if cleanup_preview.get("cleanup_blockers"):
            raise CachedGoldError(
                "PREFLIGHT_CLEANUP_BLOCKED",
                details={"blockers": cleanup_preview["cleanup_blockers"]},
            )
        runtime["preflight_cleanup"] = runtime["gold"].cleanup(
            runtime["manifest"],
            apply=True,
            expected_hash=cleanup_preview["cleanup_plan_hash"],
        )
        _refresh_runtime_test_doubles(runtime, relay_port=args.relay_port)
        stack.enter_context(_isolated_backend(runtime["app_main"].app, port=args.api_port))
        _refresh_runtime_test_doubles(runtime, relay_port=args.relay_port)
        if args.command == "doctor":
            return _doctor(runtime, live=args.live)
        result = runtime["gold"].run_suite(
            runtime["manifest"],
            args.confirm_suite,
            args.message_id,
        )
        return _augment_result(runtime, result)


def _cache_status(args: argparse.Namespace) -> dict[str, Any]:
    manifest = Path(args.manifest).resolve()
    payload = _read_json(manifest)
    root = (
        PROJECT_ROOT
        / "test-results"
        / "cached-gold-business-regression"
        / str(payload["suite_id"])
        / "cache-v1"
    )
    ai_entries = list((root / "ai").glob("**/*.json")) if (root / "ai").exists() else []
    object_entries = list((root / "objects").glob("**/*.bin")) if (root / "objects").exists() else []
    corrupt = [path for path in ai_entries if ".corrupt-" in path.name]
    return {
        "status": "available",
        "suite_id": payload["suite_id"],
        "ai_entries": len(ai_entries) - len(corrupt),
        "corrupt_ai_entries": len(corrupt),
        "object_entries": len(object_entries),
        "cache_root": str(root),
    }


def _report(args: argparse.Namespace) -> dict[str, Any]:
    _configure_environment(args.api_port)
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))
    from tools import run_gold_mail_regression as gold

    manifest = Path(args.manifest).resolve()
    replay_root = PROJECT_ROOT / "test-results" / "cached-gold-business-regression"
    gold.EVIDENCE_ROOT = replay_root
    gold.ARCHIVE_DOC = replay_root / _read_json(manifest)["suite_id"] / "archive.md"
    result = gold.report(manifest)
    latest = Path(result["latest_result"])
    persisted = _read_json(latest)
    result["resource_usage"] = persisted.get("resource_usage")
    return result


def _self_test() -> dict[str, Any]:
    from pydantic import BaseModel
    from app.integrations.ai_provider import AiExtractResponse, AiJsonCompletion

    class Response(BaseModel):
        value: str

    calls = 0

    async def provider(**kwargs: Any) -> AiJsonCompletion[Any]:
        nonlocal calls
        calls += 1
        parsed = kwargs["response_model"](value="cached")
        return AiJsonCompletion(
            trace_id="live", request_payload={}, response_payload={"ok": True},
            output_text='{"value":"cached"}', parsed=parsed, latency_ms=1,
            task="self_test", route_name="fake", provider_name="fake", model_name="fake",
        )

    with tempfile.TemporaryDirectory(prefix="cached-gold-self-test-") as temporary:
        cache = AiCompletionCache(Path(temporary), provider, refresh=False, max_live_calls=1)

        async def exercise() -> None:
            first = await cache.invoke_structured(
                task="self_test", messages=[{"role": "user", "content": "hash-only"}],
                response_model=Response, temperature=0,
            )
            second = await cache.invoke_structured(
                task="self_test", messages=[{"role": "user", "content": "hash-only"}],
                response_model=Response, temperature=0,
            )
            if first.parsed.value != "cached" or second.parsed.value != "cached":
                raise CachedGoldError("SELF_TEST_PARSE_FAILED")

        asyncio.run(exercise())
        if calls != 1 or cache.live_calls != 1 or cache.hits != 1:
            raise CachedGoldError("SELF_TEST_CACHE_REUSE_FAILED", details=cache.summary())
        overlay_cache = AiCompletionCache(
            Path(temporary) / "overlay",
            provider,
            refresh=False,
            max_live_calls=0,
            gold_fixtures=[
                {
                    "intent": "new_repair",
                    "fields": {"customer_name": "Gold Customer"},
                    "items": [
                        {
                            "sn": "SN-GOLD-001",
                            "board_name": "BOARD-GOLD",
                            "material_code": "MAT-MUST-NOT-PASS",
                        },
                        {"sn": "SN-GOLD-002", "board_name": "BOARD-GOLD-2"},
                    ],
                    "sns": ["SN-GOLD-001", "SN-GOLD-002"],
                    "missing_fields": [],
                }
            ],
        )
        overlaid = overlay_cache._apply_gold_fixture(
            task_name="repair_field_extract",
            messages=[{"role": "user", "content": "SN-GOLD-001"}],
            parsed=AiExtractResponse(missing_fields={"customer_name": "missing"}),
        )
        if (
            overlaid.extracted_fields.get("customer_name") != "Gold Customer"
            or overlaid.extracted_items[0].get("board_name") != "BOARD-GOLD"
            or "material_code" in overlaid.extracted_items[0]
            or overlaid.missing_fields
            or overlay_cache.gold_overlays != 1
        ):
            raise CachedGoldError("SELF_TEST_GOLD_OVERLAY_FAILED")
        return {"status": "passed", "provider_calls": calls, "cache": cache.summary()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gold business regression with real IMAP/SMTP, cached AI, and local object storage"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "run", "cache-status", "report"):
        item = sub.add_parser(name)
        item.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        item.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
        if name in {"doctor", "run"}:
            item.add_argument("--relay-port", type=int, default=DEFAULT_RELAY_PORT)
        if name == "doctor":
            item.add_argument("--live", action="store_true")
        if name == "run":
            item.add_argument("--confirm-suite", required=True)
            item.add_argument("--message-id", action="append")
            item.add_argument("--refresh-ai-cache", action="store_true")
            item.add_argument("--max-live-ai-calls", type=int, default=20)
            item.add_argument("--wait-timeout-seconds", type=int, default=300)
    sub.add_parser("self-test")
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args()
    try:
        if args.command in {"doctor", "run"}:
            result = _run(args)
        elif args.command == "cache-status":
            result = _cache_status(args)
        elif args.command == "report":
            result = _report(args)
        elif args.command == "self-test":
            if str(BACKEND_ROOT) not in sys.path:
                sys.path.insert(0, str(BACKEND_ROOT))
            result = _self_test()
        else:
            raise CachedGoldError("COMMAND_UNSUPPORTED")
        print(json.dumps({"ok": True, "command": args.command, "data": result}, ensure_ascii=False, indent=2, default=str))
        if isinstance(result, dict) and result.get("status") in {"blocked", "failed", "error"}:
            raise SystemExit(2)
    except CachedGoldError as exc:
        print(json.dumps({"ok": False, "command": getattr(args, "command", None), "error": {"code": exc.code, "details": exc.details}}, ensure_ascii=False, indent=2))
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "command": getattr(args, "command", None), "error": {"code": "INTERRUPTED"}}, ensure_ascii=False, indent=2))
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
