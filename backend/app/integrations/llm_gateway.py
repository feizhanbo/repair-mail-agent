from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import yaml
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.integrations.ai_provider import AiJsonCompletion, AiProviderError, _normalize_response_payload

T = TypeVar("T", bound=BaseModel)


class LlmTask(StrEnum):
    MAIL_CLASSIFICATION = "mail_classification"
    REPAIR_FIELD_EXTRACT = "repair_field_extract"
    REPLY_DRAFT = "reply_draft"
    ATTACHMENT_TEXT_PARSE = "attachment_text_parse"
    ATTACHMENT_VISUAL_PARSE = "attachment_visual_parse"


@dataclass(frozen=True)
class LlmEndpoint:
    profile: str
    model: str
    api_key: str
    base_url: str


@dataclass(frozen=True)
class LlmRoute:
    task: LlmTask
    primary: LlmEndpoint
    fallback: LlmEndpoint | None
    temperature: float
    timeout_seconds: float
    max_retries: int
    max_output_tokens: int | None
    requires_json: bool
    requires_vision: bool


def _setting_value(name: str) -> str:
    return str(getattr(settings, name, None) or "").strip()


def _endpoint(raw: Any, profiles: dict[str, Any], *, task: LlmTask, label: str) -> LlmEndpoint:
    if not isinstance(raw, dict):
        raise ValueError(f"LLM_ROUTE_{task.value.upper()}_{label.upper()}_INVALID")
    profile_name = str(raw.get("profile") or "").strip()
    model = str(raw.get("model") or "").strip()
    profile = profiles.get(profile_name)
    if not profile_name or not model or not isinstance(profile, dict):
        raise ValueError(f"LLM_ROUTE_{task.value.upper()}_{label.upper()}_INVALID")
    api_key = _setting_value(str(profile.get("api_key_setting") or ""))
    base_url = _setting_value(str(profile.get("base_url_setting") or "")).rstrip("/")
    if not base_url:
        raise ValueError(f"LLM_PROFILE_{profile_name.upper()}_NOT_CONFIGURED")
    return LlmEndpoint(profile=profile_name, model=model, api_key=api_key, base_url=base_url)


def load_llm_routes() -> dict[LlmTask, LlmRoute]:
    path = Path(settings.LLM_ROUTES_FILE)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("LLM_ROUTES_FILE_INVALID") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict) or not isinstance(raw.get("tasks"), dict):
        raise ValueError("LLM_ROUTES_FILE_INVALID")
    profiles = raw["profiles"]
    task_rows = raw["tasks"]
    routes: dict[LlmTask, LlmRoute] = {}
    for task in LlmTask:
        row = task_rows.get(task.value)
        if not isinstance(row, dict):
            raise ValueError(f"LLM_ROUTE_{task.value.upper()}_MISSING")
        primary = _endpoint(row.get("primary"), profiles, task=task, label="primary")
        fallback_raw = row.get("fallback")
        fallback = _endpoint(fallback_raw, profiles, task=task, label="fallback") if fallback_raw else None
        requires_vision = bool(row.get("requires_vision", False))
        if requires_vision and not any(marker in primary.model.lower() for marker in ("vl", "vision")):
            raise ValueError(f"LLM_ROUTE_{task.value.upper()}_VISION_MODEL_REQUIRED")
        routes[task] = LlmRoute(
            task=task, primary=primary, fallback=fallback,
            temperature=float(row.get("temperature", 0.1)),
            timeout_seconds=float(row.get("timeout_seconds", settings.AI_TIMEOUT_SECONDS)),
            max_retries=max(0, int(row.get("max_retries", 0))),
            max_output_tokens=int(row["max_output_tokens"]) if row.get("max_output_tokens") else None,
            requires_json=bool(row.get("requires_json", True)), requires_vision=requires_vision,
        )
    return routes


def public_llm_routes() -> dict[str, Any]:
    return {
        task.value: {
            "primary": {"profile": route.primary.profile, "model": route.primary.model},
            "fallback": ({"profile": route.fallback.profile, "model": route.fallback.model} if route.fallback else None),
            "timeout_seconds": route.timeout_seconds,
            "max_retries": route.max_retries,
            "requires_vision": route.requires_vision,
        }
        for task, route in load_llm_routes().items()
    }


def llm_task_configured(task: LlmTask) -> bool:
    route = load_llm_routes()[task]
    return bool(route.primary.api_key or (route.fallback and route.fallback.api_key))


def _error(provider: str, code: str, exc: Exception) -> AiProviderError:
    error = AiProviderError(f"{provider.upper()}_PROVIDER_{code}")
    error.original_exception_type = exc.__class__.__name__  # type: ignore[attr-defined]
    return error


def _map_invocation_error(provider: str, exc: Exception) -> AiProviderError:
    name = exc.__class__.__name__.upper()
    status_code = getattr(exc, "status_code", None)
    if "TIMEOUT" in name:
        return _error(provider, "TIMEOUT", exc)
    if status_code is not None:
        return _error(provider, f"HTTP_{status_code}", exc)
    if any(marker in name for marker in ("CONNECT", "CONNECTION", "APIERROR")):
        return _error(provider, "REQUEST_FAILED", exc)
    return _error(provider, "REQUEST_FAILED", exc)


def _fallback_allowed(exc: AiProviderError) -> bool:
    value = str(exc).upper()
    if any(marker in value for marker in ("TIMEOUT", "RATE_LIMIT", "HTTP_429", "REQUEST_FAILED")):
        return True
    return any(f"HTTP_{status}" in value for status in range(500, 600))


def _response_payload(raw: Any) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": getattr(raw, "content", None)}}],
        "response_metadata": getattr(raw, "response_metadata", None) or {},
        "usage": getattr(raw, "usage_metadata", None) or {},
        "id": getattr(raw, "id", None),
    }


async def _invoke_endpoint(
    *, route: LlmRoute, endpoint: LlmEndpoint, route_attempt: int,
    messages: list[dict[str, Any]], response_model: type[T], temperature: float | None,
) -> AiJsonCompletion[T]:
    actual_temperature = route.temperature if temperature is None else temperature
    if not endpoint.api_key:
        error = AiProviderError(f"{endpoint.profile.upper()}_API_KEY_NOT_CONFIGURED")
        error.route_name = endpoint.profile  # type: ignore[attr-defined]
        error.model_name = endpoint.model  # type: ignore[attr-defined]
        error.route_attempt = route_attempt  # type: ignore[attr-defined]
        raise error
    request_payload = {
        "model": endpoint.model,
        "messages": [{"role": str(item.get("role") or "user"), "content": item.get("content")} for item in messages],
        "temperature": actual_temperature, "response_format": {"type": "json_object"},
        "task": route.task.value, "framework": "langchain", "route": endpoint.profile,
        "route_attempt": route_attempt,
    }
    kwargs: dict[str, Any] = {"max_tokens": route.max_output_tokens} if route.max_output_tokens else {}
    model = ChatOpenAI(
        api_key=endpoint.api_key, base_url=endpoint.base_url, model=endpoint.model,
        timeout=route.timeout_seconds, max_retries=0, temperature=actual_temperature, **kwargs,
    )
    started = time.perf_counter()
    try:
        result = await model.with_structured_output(method="json_mode", include_raw=True).ainvoke(messages)
    except Exception as exc:
        error = _map_invocation_error(endpoint.profile, exc)
        error.request_payload = request_payload  # type: ignore[attr-defined]
        error.route_name = endpoint.profile  # type: ignore[attr-defined]
        error.model_name = endpoint.model  # type: ignore[attr-defined]
        error.route_attempt = route_attempt  # type: ignore[attr-defined]
        raise error from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    trace_id = uuid.uuid4().hex
    raw = result.get("raw") if isinstance(result, dict) else None
    parsed_value = result.get("parsed") if isinstance(result, dict) else None
    parsing_error = result.get("parsing_error") if isinstance(result, dict) else None
    response_payload = _response_payload(raw)
    output_text = getattr(raw, "content", "") if raw is not None else ""
    if isinstance(output_text, list):
        output_text = json.dumps(output_text, ensure_ascii=False)
    if isinstance(parsed_value, dict):
        parsed_json = parsed_value
        output_text = output_text or json.dumps(parsed_value, ensure_ascii=False)
    else:
        try:
            parsed_json = json.loads(str(output_text))
        except json.JSONDecodeError as exc:
            error = _error(endpoint.profile, "OUTPUT_NOT_JSON", exc)
            error.raw_output = str(output_text)  # type: ignore[attr-defined]
            error.trace_id = trace_id  # type: ignore[attr-defined]
            error.request_payload = request_payload  # type: ignore[attr-defined]
            error.response_payload = response_payload  # type: ignore[attr-defined]
            error.latency_ms = latency_ms  # type: ignore[attr-defined]
            raise error from exc
    try:
        parsed = response_model.model_validate(_normalize_response_payload(parsed_json, response_model))
    except ValidationError as exc:
        error = _error(endpoint.profile, "OUTPUT_SCHEMA_INVALID", exc)
        error.raw_output = str(output_text)  # type: ignore[attr-defined]
        error.trace_id = trace_id  # type: ignore[attr-defined]
        error.request_payload = request_payload  # type: ignore[attr-defined]
        error.response_payload = response_payload  # type: ignore[attr-defined]
        error.latency_ms = latency_ms  # type: ignore[attr-defined]
        error.route_name = endpoint.profile  # type: ignore[attr-defined]
        error.model_name = endpoint.model  # type: ignore[attr-defined]
        error.route_attempt = route_attempt  # type: ignore[attr-defined]
        raise error from exc
    if parsing_error is not None:
        error = _error(endpoint.profile, "OUTPUT_SCHEMA_INVALID", parsing_error)
        error.route_name = endpoint.profile  # type: ignore[attr-defined]
        error.model_name = endpoint.model  # type: ignore[attr-defined]
        error.route_attempt = route_attempt  # type: ignore[attr-defined]
        raise error
    return AiJsonCompletion(
        trace_id=trace_id, request_payload=request_payload, response_payload=response_payload,
        output_text=str(output_text), parsed=parsed, latency_ms=latency_ms,
        task=route.task.value, route_name=endpoint.profile, provider_name=endpoint.profile,
        model_name=endpoint.model, route_attempt=route_attempt, fallback_used=route_attempt > route.max_retries + 1,
    )


async def invoke_structured(
    *, task: LlmTask, messages: list[dict[str, Any]], response_model: type[T], temperature: float | None = None,
) -> AiJsonCompletion[T]:
    route = load_llm_routes()[task]
    endpoints = [route.primary] + ([route.fallback] if route.fallback else [])
    last_error: AiProviderError | None = None
    route_attempt = 0
    for endpoint_index, endpoint in enumerate(endpoints):
        if endpoint is None:
            continue
        for retry in range(route.max_retries + 1):
            route_attempt += 1
            try:
                return await _invoke_endpoint(
                    route=route, endpoint=endpoint, route_attempt=route_attempt,
                    messages=messages, response_model=response_model, temperature=temperature,
                )
            except AiProviderError as exc:
                last_error = exc
                if not _fallback_allowed(exc):
                    raise
                if retry < route.max_retries:
                    await asyncio.sleep(min(2 ** retry, 4))
                    continue
                if endpoint_index == len(endpoints) - 1:
                    raise
    assert last_error is not None
    raise last_error
