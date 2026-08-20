from __future__ import annotations

import asyncio
import json
import logging
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ai.prompts import PROMPTS, REPAIR_FIELD_EXTRACT, REPLY_DRAFT
from app.core.email_classification import CLASSIFICATION_VERSION, decision_for_intent, normalize_intent
from app.core.repair_items import (
    canonical_sn,
    normalize_board_code,
    normalize_repair_item,
    normalize_repair_items,
)
from app.core.request_context import get_correlation_id
from app.integrations.ai_provider import AiExtractResponse, AiProviderError, AiReplyDraftResponse
from app.integrations.llm_gateway import LlmTask, invoke_structured, llm_task_configured
from app.models import AiCallLog, Email, EmailAttachment, EmailThread, OssObject, ParseResult, RepairTicket, RepairTicketItem, SnAsset
from app.services.business_rules import required_missing_for_values
from app.services.common import sha256_text, to_plain, utcnow
from app.services.logging_safety import safe_error_code
from app.services.parser import clean_email_body

logger = logging.getLogger(__name__)
_ai_log_file_lock = asyncio.Lock()


def _is_retryable_error(exc: AiProviderError) -> bool:
    msg = str(exc)
    if "TIMEOUT" in msg.upper():
        return True
    if "OUTPUT_NOT_JSON" in msg or "OUTPUT_SCHEMA_INVALID" in msg:
        return True
    m = re.search(r"HTTP_([0-9]+)", msg)
    if m and int(m.group(1)) >= 500:
        return True
    return False


def text_ai_configured() -> bool:
    return llm_task_configured(LlmTask.REPAIR_FIELD_EXTRACT)


def multimodal_ai_configured() -> bool:
    return llm_task_configured(LlmTask.ATTACHMENT_VISUAL_PARSE)


def ai_configured() -> bool:
    return text_ai_configured()


def _compact_text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    normalized = value.strip()
    return normalized[:limit]


_BODY_TOKEN_PATTERN = re.compile(r"\b[A-Z0-9][A-Z0-9._-]{7,99}\b", re.IGNORECASE)
_EMBEDDED_SN_PATTERN = re.compile(
    r"M[A-Z0-9]{13,20}(?=(?:校准|自检|FAIL|异常|故障|损坏|[,，。;；\s]|$))",
    re.IGNORECASE,
)
_RETURN_CONTEXT_PATTERNS = (
    re.compile(r"(?:维修返回地址|返修寄回地址|维修后寄回地址|寄回地址|收件地址|邮寄地址)\s*[:：]?", re.IGNORECASE),
    re.compile(r"(?:shipping information after repaired|send back to|return address)\s*[:：]?", re.IGNORECASE),
)
_FIELD_LABEL_PATTERNS = {
    "contact_person": re.compile(r"(?:寄回联系人|收件人|联系人|contact|attn)\s*[:：]?", re.IGNORECASE),
    "contact_phone": re.compile(r"(?:寄回联系电话|联系电话|联系方式|电话|手机|tel|phone|mob)\s*[/A-Za-z]*\s*[:：]?", re.IGNORECASE),
}
_SUPPLEMENT_PHONE_PATTERN = re.compile(
    r"(?:寄回联系电话|联系电话|联系方式|电话|手机|tel(?:ephone)?|phone|mobile)"
    r"[ \t]*[:：]?[ \t]*(\+?\d[\d \t()\-]{5,28}\d)",
    re.IGNORECASE,
)
_EXPLICIT_ENGLISH_ADDRESS_PATTERN = re.compile(
    r"(?:^|\n)[ \t]*(?:addr(?:ess)?|shipping[ \t]+address)"
    r"[ \t]*[:：][ \t]*([^\r\n]{8,220})"
    r"(?:\r?\n[ \t]*ZIP[ \t]*[:：][ \t]*([^\r\n]{3,20}))?",
    re.IGNORECASE,
)


def _apply_deterministic_explicit_return_fields(
    *, fields: dict[str, Any], email: Email, evidence: dict[str, Any],
    field_confidences: dict[str, float]
) -> None:
    """Preserve explicit current-message labels when the model paraphrases them."""
    body = clean_email_body(email)
    address_match = _EXPLICIT_ENGLISH_ADDRESS_PATTERN.search(body)
    if address_match:
        address = address_match.group(1).strip()
        postal_code = (address_match.group(2) or "").strip()
        if postal_code:
            address = f"{address} ZIP: {postal_code}"
        fields["mailing_address"] = address
        field_confidences["mailing_address"] = 1.0
        evidence.setdefault("derived_fields", {})["mailing_address"] = {
            "source": "explicit_english_address_label"
        }


def _apply_deterministic_supplement_fields(
    *, fields: dict[str, Any], email: Email, evidence: dict[str, Any],
    field_confidences: dict[str, float]
) -> None:
    """Prefer explicit labels in the customer's latest reply over AI omission."""
    body = clean_email_body(email)
    phone_match = _SUPPLEMENT_PHONE_PATTERN.search(body)
    if phone_match:
        fields["contact_phone"] = re.sub(
            r"\s+", "", phone_match.group(1).strip()
        )
        field_confidences["contact_phone"] = 1.0
        evidence.setdefault("derived_fields", {})["contact_phone"] = {
            "source": "explicit_supplement_label"
        }


def _normalize_customer_mailing_address(value: Any) -> Any:
    """Remove only an immediately repeated municipality prefix.

    This deliberately avoids broad address rewriting: the source mail remains
    unchanged and only an unambiguous adjacent duplication is normalized.
    """
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    for municipality in ("北京市", "上海市", "天津市", "重庆市"):
        normalized = re.sub(
            rf"^(?:{re.escape(municipality)}){{2,}}",
            municipality,
            normalized,
        )
    return normalized


def _return_context(body: str) -> str:
    starts = [
        match.start()
        for pattern in _RETURN_CONTEXT_PATTERNS
        for match in pattern.finditer(body)
    ]
    if not starts:
        return ""
    # A return-information declaration normally covers the remaining contact
    # block. Limit its size so unrelated quoted history cannot become evidence.
    return body[min(starts) : min(len(body), min(starts) + 1200)]


def _sanitize_customer_return_fields(
    *,
    fields: dict[str, Any],
    email: Email,
    evidence: dict[str, Any],
    field_confidences: dict[str, float],
) -> None:
    """Reject signature-only customer return details after AI extraction."""
    body = clean_email_body(email)
    if not body:
        return
    context = _return_context(body)
    rejected: list[str] = []
    accepted: list[str] = []
    for name in ("mailing_address", "contact_person", "contact_phone"):
        value = str(fields.get(name) or "").strip()
        if not value:
            continue
        value_present = value.casefold() in context.casefold()
        if name == "mailing_address":
            supported = bool(
                context
                and (
                    value_present
                    or re.search(
                        r"(?:^|\n)\s*(?:addr(?:ess)?|ship(?:ping)? address)\s*[:：]",
                        context,
                        re.IGNORECASE,
                    )
                )
            )
        elif name == "contact_person":
            supported = bool(
                context
                and value_present
                and (
                    _FIELD_LABEL_PATTERNS[name].search(context)
                    or re.search(r"shipping information after repaired|send back to", context, re.IGNORECASE)
                )
            )
        else:
            supported = bool(
                context
                and value_present
                and _FIELD_LABEL_PATTERNS[name].search(context)
            )
        if not supported:
            fields.pop(name, None)
            rejected.append(name)
        else:
            accepted.append(name)
            field_confidences[name] = 1.0
    if rejected:
        evidence.setdefault("derived_fields", {})["rejected_signature_only_fields"] = {
            "fields": rejected,
            "reason": "explicit_customer_return_context_required",
        }
    if accepted:
        evidence.setdefault("derived_fields", {})["accepted_customer_return_fields"] = {
            "fields": accepted,
            "reason": "explicit_customer_return_context",
        }


async def _replace_ai_sns_with_known_body_assets(
    session: AsyncSession,
    *,
    email: Email,
    items: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prefer exact, valid SN master hits found in the body over AI column guesses."""
    body = clean_email_body(email)
    tokens = list(
        dict.fromkeys(
            [match.group(0).upper() for match in _BODY_TOKEN_PATTERN.finditer(body)]
            + [match.group(0).upper() for match in _EMBEDDED_SN_PATTERN.finditer(body)]
        )
    )
    assets: list[SnAsset] = []
    for token in tokens:
        asset = await session.scalar(select(SnAsset).where(SnAsset.sn == token))
        if asset is not None and asset.asset_status == "valid":
            assets.append(asset)
    known_sns = list(dict.fromkeys(str(asset.sn).strip().upper() for asset in assets))
    actual_sns = [canonical_sn(item) for item in items if canonical_sn(item)]
    if not known_sns or known_sns == actual_sns:
        return items
    if len(known_sns) != len(items):
        return items
    corrected: list[dict[str, Any]] = []
    for item, sn in zip(items, known_sns, strict=True):
        row = dict(item)
        row["sn"] = sn
        # If the AI put the actual SN into board_code because it shifted a
        # flattened table column, that value is not a valid board code.
        if normalize_board_code(row.get("board_code")) in set(known_sns):
            row.pop("board_code", None)
        corrected.append(row)
    evidence.setdefault("derived_fields", {})["sn_list"] = {
        "source": "valid_sn_assets_present_in_email_body",
        "sn_count": len(known_sns),
        "replaced_ai_candidates": actual_sns,
    }
    return corrected


def _safe_json(value: Any) -> str:
    return json.dumps(to_plain(value), ensure_ascii=False, default=str)


_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "password", "token", "access_token", "bearer_token",
    "secret", "access_key", "secret_key",
    "oss_access_key", "oss_secret_key", "smtp_password", "imap_password",
}


def sanitize_ai_detail(value: Any, *, key: str | None = None) -> Any:
    normalized_key = (key or "").lower()
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(("_api_key", "_password", "_secret_key")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): sanitize_ai_detail(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_ai_detail(item) for item in value]
    if isinstance(value, str):
        if value.lower().startswith("data:"):
            mime = value[5:].split(";", 1)[0][:100]
            return {"binary_ref": True, "mime_type": mime, "chars": len(value), "sha256": sha256_text(value)}
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment:
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) + "#SIGNED_QUERY_REDACTED"
    return to_plain(value)


def _write_jsonl(record: dict[str, Any]) -> tuple[str, int, str]:
    now = utcnow()
    log_dir = Path(settings.AI_LOG_DIR) / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ai-{now:%Y%m%d}.jsonl"
    line_no = 1
    if log_path.exists():
        with log_path.open("r", encoding="utf-8") as existing:
            line_no = sum(1 for _ in existing) + 1
    line = _safe_json(record)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
    return log_path.as_posix(), line_no, sha256_text(line)


def _key_result(call_type: str, parsed: BaseModel | None) -> dict[str, Any] | None:
    if parsed is None:
        return None
    data = parsed.model_dump()
    if call_type == "field_extract":
        return {
            "intent_type": data.get("intent_type"),
            "field_keys": sorted((data.get("extracted_fields") or {}).keys()),
            "item_count": len(data.get("extracted_items") or []),
            "missing_field_keys": sorted((data.get("missing_fields") or {}).keys()),
            "conflict_field_keys": sorted((data.get("conflict_fields") or {}).keys()),
        }
    if call_type == "generate_reply_draft":
        body = data.get("body") or ""
        return {
            "subject_chars": len(data.get("subject") or ""),
            "body_chars": len(body),
            "risk_level": data.get("risk_level"),
            "missing_field_keys": sorted((data.get("missing_fields") or {}).keys()),
            "suggestion_count": len(data.get("suggestions") or []),
        }
    return {
        "result_keys": sorted(data.keys()),
        "warning_count": len(data.get("warnings") or []),
        "item_count": len(data.get("extracted_items") or []),
        "truncated": bool(data.get("truncated")),
    }


def _confidence(parsed: BaseModel | None) -> float | None:
    if parsed is None:
        return None
    value = getattr(parsed, "confidence_score", None)
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _status_for(parsed: BaseModel | None, error: str | None) -> str:
    if error:
        return "failed"
    confidence = _confidence(parsed)
    if confidence is not None and confidence < settings.CONFIDENCE_THRESHOLD:
        return "low_confidence"
    return "success"


def _payload_metadata(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    serialized = _safe_json(payload)
    return {
        "keys": sorted(str(key) for key in payload.keys()),
        "chars": len(serialized),
        "sha256": sha256_text(serialized),
    }


def _token_usage(response_payload: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    usage = (response_payload or {}).get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    return (
        int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
        int(output_tokens) if isinstance(output_tokens, (int, float)) else None,
        int(total_tokens) if isinstance(total_tokens, (int, float)) else None,
    )


def _ai_call_context(call_type: str) -> tuple[str, str]:
    mapping = {
        "field_extract": ("邮件级 DeepSeek 结构化解析", "抽取业务字段和明细"),
        "generate_reply_draft": ("DeepSeek 回复草稿生成", "生成客户回复草稿"),
        "attachment_text_parse": ("Qwen 文本类附件解析", "解析文本、表格或文档附件"),
        "attachment_visual_parse": ("Qwen 图片/PDF 多模态解析", "解析图片或扫描 PDF 附件"),
    }
    return mapping.get(call_type, ("AI 调用", call_type))


def _ai_problem_reason_and_action(status: str, error_code: str | None) -> tuple[str, str]:
    code = (error_code or "").upper()
    if status == "low_confidence":
        return "模型返回结果置信度低，不能自动应用", "人工复核解析结果，必要时补充邮件正文或附件信息"
    if "NOT_CONFIGURED" in code or "API_KEY" in code:
        return "AI 服务配置缺失或不完整", "检查 .env 中对应 provider 的 key、model 和 base_url"
    if "429" in code or "RATE" in code or "LIMIT" in code:
        return "模型服务限流", "稍后重试，或降低并发和重试频率"
    if "TIMEOUT" in code:
        return "模型调用超时", "检查网络、附件大小和模型响应耗时后重试"
    if "HTTP_5" in code:
        return "模型服务端异常", "稍后重试，若持续失败则检查服务商状态"
    if "INVALID_RESPONSE_JSON" in code or "OUTPUT_NOT_JSON" in code or "SCHEMA" in code:
        return "模型输出不是有效的项目 JSON 结构", "查看 JSONL 详情，优化 prompt 和输出格式约束"
    if status == "failed":
        return f"AI 调用失败，错误码 {error_code or 'UNKNOWN'}", "查看 JSONL 详情和系统配置后重试"
    return "AI 调用完成", "无需处理"


def ai_log_diagnostics(ai_log: AiCallLog) -> dict[str, str]:
    stage, action = _ai_call_context(ai_log.call_type)
    reason, suggestion = _ai_problem_reason_and_action(ai_log.status, ai_log.error_code or ai_log.error_message)
    provider = ai_log.provider_name or "unknown"
    model = ai_log.model_name or "unknown"
    if ai_log.status == "success":
        description = f"模型 {provider}/{model} 在 {stage} 执行 {action} 已成功完成。"
    else:
        description = f"模型 {provider}/{model} 在 {stage} 执行 {action} 时失败：{reason}。建议：{suggestion}。"
    return {
        "ai_stage": stage,
        "ai_action": action,
        "problem_reason": reason,
        "resolution_suggestion": suggestion,
        "problem_description": description,
    }


async def persist_ai_log(
    session: AsyncSession,
    *,
    trace_id: str,
    call_type: str,
    input_payload: dict[str, Any],
    request_payload: dict[str, Any] | None,
    output_payload: dict[str, Any] | None,
    parsed: BaseModel | None,
    latency_ms: int | None,
    input_summary: str,
    output_summary: str | None,
    email_id: int | None = None,
    ticket_id: int | None = None,
    attachment_id: int | None = None,
    job_run_id: int | None = None,
    mail_fetch_record_id: int | None = None,
    correlation_id: str | None = None,
    provider_name: str = "deepseek",
    model_name: str | None = None,
    prompt_version: str | None = None,
    prompt_hash: str | None = None,
    route_name: str | None = None,
    route_attempt: int = 1,
    fallback_used: bool = False,
    attempt_count: int = 1,
    error_message: str | None = None,
) -> AiCallLog:
    model_name = model_name or settings.AI_MODEL
    prompt_version = prompt_version or settings.AI_PROMPT_VERSION
    error_code = safe_error_code(error_message, "AI_CALL_FAILED")
    input_tokens, output_tokens, total_tokens = _token_usage(output_payload)
    record = {
        "trace_id": trace_id,
        "correlation_id": correlation_id or get_correlation_id(),
        "call_type": call_type,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "route_name": route_name,
        "route_attempt": route_attempt,
        "fallback_used": fallback_used,
        "provider": provider_name,
        "model": model_name,
        "email_id": email_id,
        "ticket_id": ticket_id,
        "attachment_id": attachment_id,
        "job_run_id": job_run_id,
        "mail_fetch_record_id": mail_fetch_record_id,
        "input_metadata": _payload_metadata(input_payload),
        "request_metadata": _payload_metadata(request_payload),
        "response_metadata": _payload_metadata(output_payload),
        "parsed_key_result": _key_result(call_type, parsed),
        "latency_ms": latency_ms,
        "status": _status_for(parsed, error_message),
        "error_code": error_code,
        "attempt_count": attempt_count,
        "token_usage": {
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens,
        },
        "created_at": utcnow().isoformat(),
    }
    if settings.AI_FULL_LOG_ENABLED:
        record.update(
            {
                "input_payload": sanitize_ai_detail(input_payload),
                "request_payload": sanitize_ai_detail(request_payload),
                "response_payload": sanitize_ai_detail(output_payload),
                "parsed_result": sanitize_ai_detail(parsed.model_dump() if parsed else None),
            }
        )
    async with _ai_log_file_lock:
        log_file_path, line_no, record_hash = await asyncio.to_thread(_write_jsonl, record)
    ai_log = AiCallLog(
        trace_id=trace_id,
        email_id=email_id,
        ticket_id=ticket_id,
        attachment_id=attachment_id,
        job_run_id=job_run_id,
        mail_fetch_record_id=mail_fetch_record_id,
        correlation_id=correlation_id or get_correlation_id(),
        call_type=call_type,
        provider_name=provider_name,
        model_name=model_name,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        route_name=route_name,
        route_attempt=route_attempt,
        fallback_used=fallback_used,
        input_summary=input_summary[:1000],
        output_summary=(output_summary or "")[:1000] or None,
        parsed_key_result=_key_result(call_type, parsed),
        confidence_score=_confidence(parsed),
        latency_ms=latency_ms,
        attempt_count=attempt_count,
        error_code=error_code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        status=_status_for(parsed, error_message),
        error_message=error_code,
        log_file_path=log_file_path,
        log_line_no=line_no,
        log_record_hash=record_hash,
    )
    session.add(ai_log)
    await session.flush()
    return ai_log


def _resolve_ai_log_path(log_file_path: str) -> Path:
    if not log_file_path:
        raise FileNotFoundError("AI_LOG_DETAIL_EXPIRED")
    root = Path(settings.AI_LOG_DIR).resolve()
    supplied = Path(log_file_path)
    candidates = [supplied] if supplied.is_absolute() else [Path.cwd() / supplied, root.parent.parent / supplied]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == root or root in resolved.parents:
            return resolved
    raise ValueError("AI_LOG_PATH_INVALID")


def ai_log_availability(ai_log: AiCallLog) -> str:
    if not ai_log.log_file_path or not ai_log.log_line_no:
        return "metadata_only"
    try:
        path = _resolve_ai_log_path(ai_log.log_file_path)
    except ValueError:
        return "corrupt"
    return "full" if path.exists() else "expired"


def _ai_log_detail_envelope(
    ai_log: AiCallLog,
    *,
    availability: str,
    message: str,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = record or {}
    metadata_sections = {
        "input": record.get("input_payload") if "input_payload" in record else record.get("input_metadata"),
        "request": record.get("request_payload") if "request_payload" in record else record.get("request_metadata"),
        "response": record.get("response_payload") if "response_payload" in record else record.get("response_metadata"),
        "parsed_result": record.get("parsed_result") or ai_log.parsed_key_result,
    }
    return {
        "availability": availability,
        "message": message,
        "sections": metadata_sections,
        "associations": {
            "email_id": ai_log.email_id,
            "ticket_id": ai_log.ticket_id,
            "attachment_id": ai_log.attachment_id,
            "job_run_id": ai_log.job_run_id,
            "correlation_id": ai_log.correlation_id,
            "trace_id": ai_log.trace_id,
        },
        "tokens": record.get("token_usage") or {
            "input": ai_log.input_tokens,
            "output": ai_log.output_tokens,
            "total": ai_log.total_tokens,
        },
        "metadata": {
            "call_type": ai_log.call_type,
            "provider": ai_log.provider_name,
            "model": ai_log.model_name,
            "prompt_version": ai_log.prompt_version,
            "attempt_count": ai_log.attempt_count,
            "latency_ms": ai_log.latency_ms,
            "status": ai_log.status,
            "error_code": ai_log.error_code,
            "created_at": ai_log.created_at.isoformat() if ai_log.created_at else None,
        },
    }


async def read_ai_log_detail(ai_log: AiCallLog) -> dict[str, Any]:
    availability = ai_log_availability(ai_log)
    if availability == "metadata_only":
        return _ai_log_detail_envelope(
            ai_log,
            availability="metadata_only",
            message="历史记录仅保留元数据，完整输入、请求和响应从未持久化。",
        )
    if availability == "expired":
        return _ai_log_detail_envelope(
            ai_log,
            availability="expired",
            message="完整日志已超过保留期或持久卷中不存在，仅可查看数据库元数据。",
        )
    if availability == "corrupt":
        return _ai_log_detail_envelope(
            ai_log,
            availability="corrupt",
            message="日志路径无效，出于安全原因未读取文件。",
        )

    path = _resolve_ai_log_path(ai_log.log_file_path or "")

    def _read() -> tuple[str, dict[str, Any]]:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if line_no == ai_log.log_line_no:
                    raw = line.rstrip("\r\n")
                    return raw, json.loads(raw)
        raise FileNotFoundError("AI_LOG_DETAIL_EXPIRED")

    try:
        raw, record = await asyncio.to_thread(_read)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return _ai_log_detail_envelope(
            ai_log,
            availability="corrupt",
            message="日志行缺失或 JSON 内容损坏，仅可查看数据库元数据。",
        )
    if ai_log.log_record_hash and sha256_text(raw) != ai_log.log_record_hash:
        return _ai_log_detail_envelope(
            ai_log,
            availability="corrupt",
            message="日志完整性哈希不匹配，仅可查看数据库元数据。",
        )
    sanitized = sanitize_ai_detail(record)
    full_keys = {"input_payload", "request_payload", "response_payload", "parsed_result"}
    detail_availability = "full" if full_keys & set(sanitized) else "metadata_only"
    return _ai_log_detail_envelope(
        ai_log,
        availability=detail_availability,
        message="完整 AI 调用详情可用。" if detail_availability == "full" else "该日志行仅包含元数据。",
        record=sanitized,
    )


async def maintain_ai_jsonl_logs(session: AsyncSession) -> dict[str, int]:
    root = Path(settings.AI_LOG_DIR).resolve()
    if not root.exists():
        return {"sanitized_files": 0, "deleted_files": 0}
    cutoff = utcnow().timestamp() - max(1, settings.AI_FULL_LOG_RETENTION_DAYS) * 86400
    sanitized_files = 0
    deleted_files = 0
    for path in root.rglob("*.jsonl"):
        if path.stat().st_mtime < cutoff:
            await asyncio.to_thread(path.unlink)
            deleted_files += 1
            continue

        def _sanitize_file() -> tuple[bool, list[str]]:
            original_lines = path.read_text(encoding="utf-8").splitlines()
            sanitized_lines: list[str] = []
            for line in original_lines:
                try:
                    sanitized_lines.append(_safe_json(sanitize_ai_detail(json.loads(line))))
                except (json.JSONDecodeError, TypeError):
                    sanitized_lines.append(_safe_json({"status": "invalid_legacy_record", "record_sha256": sha256_text(line)}))
            changed = original_lines != sanitized_lines
            if changed:
                temporary = path.with_suffix(".jsonl.tmp")
                temporary.write_text("\n".join(sanitized_lines) + "\n", encoding="utf-8")
                temporary.replace(path)
            return changed, sanitized_lines

        changed, lines = await asyncio.to_thread(_sanitize_file)
        if not changed:
            continue
        sanitized_files += 1
        path_values = {str(path), path.as_posix()}
        try:
            path_values.add(path.relative_to(Path.cwd()).as_posix())
        except ValueError:
            pass
        rows = (await session.execute(select(AiCallLog).where(AiCallLog.log_file_path.in_(path_values)))).scalars().all()
        for row in rows:
            if row.log_line_no and row.log_line_no <= len(lines):
                row.log_record_hash = sha256_text(lines[row.log_line_no - 1])
    return {"sanitized_files": sanitized_files, "deleted_files": deleted_files}


async def _run_ai_json(
    session: AsyncSession,
    *,
    call_type: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    input_payload: dict[str, Any],
    input_summary: str,
    email_id: int | None = None,
    ticket_id: int | None = None,
) -> tuple[BaseModel | None, AiCallLog | None]:
    if not text_ai_configured():
        return None, None

    task = LlmTask.REPLY_DRAFT if call_type == "reply_draft" else LlmTask.REPAIR_FIELD_EXTRACT
    prompt = PROMPTS[task.value]
    try:
        completion = await invoke_structured(task=task, messages=messages, response_model=response_model)
    except AiProviderError as last_error:
        trace_id = sha256_text(f"{call_type}:{utcnow().isoformat()}")[:32]
        raw_out = getattr(last_error, "raw_output", None)
        error_code = safe_error_code(last_error, "AI_CALL_FAILED")
        ai_log = await persist_ai_log(
            session,
            trace_id=trace_id,
            call_type=call_type,
            input_payload=input_payload,
            request_payload=getattr(last_error, "request_payload", {"messages": messages}),
            output_payload={"error": str(last_error), "raw_output": raw_out if raw_out else None},
            parsed=None,
            latency_ms=None,
            input_summary=input_summary,
            output_summary=error_code,
            email_id=email_id,
            ticket_id=ticket_id,
            provider_name=str(getattr(last_error, "route_name", "unknown")),
            model_name=str(getattr(last_error, "model_name", "unknown")),
            prompt_version=prompt.version,
            prompt_hash=prompt.content_hash,
            route_name=getattr(last_error, "route_name", None),
            route_attempt=int(getattr(last_error, "route_attempt", 1)),
            fallback_used=int(getattr(last_error, "route_attempt", 1)) > 1,
            attempt_count=int(getattr(last_error, "route_attempt", 1)),
            error_message=error_code,
        )
        return None, ai_log

    parsed = completion.parsed
    output_summary = _summarize_output(call_type, parsed)
    ai_log = await persist_ai_log(
        session,
        trace_id=completion.trace_id,
        call_type=call_type,
        input_payload=input_payload,
        request_payload=completion.request_payload,
        output_payload=completion.response_payload,
        parsed=parsed,
        latency_ms=completion.latency_ms,
        input_summary=input_summary,
        output_summary=output_summary,
        email_id=email_id,
        ticket_id=ticket_id,
        provider_name=completion.provider_name or "unknown",
        model_name=completion.model_name or "unknown",
        prompt_version=prompt.version,
        prompt_hash=prompt.content_hash,
        route_name=completion.route_name,
        route_attempt=completion.route_attempt,
        fallback_used=completion.fallback_used,
        attempt_count=completion.route_attempt,
    )
    return parsed, ai_log


def _summarize_output(call_type: str, parsed: BaseModel) -> str:
    if isinstance(parsed, AiExtractResponse):
        return (
            f"intent={parsed.intent_type}; confidence={parsed.confidence_score}; "
            f"fields={','.join(sorted(parsed.extracted_fields.keys())) or '-'}; "
            f"missing={','.join(sorted(parsed.missing_fields.keys())) or '-'}"
        )
    if isinstance(parsed, AiReplyDraftResponse):
        return f"subject_chars={len(parsed.subject)}; confidence={parsed.confidence_score}; risk={parsed.risk_level}"
    return f"{call_type} completed"


def _email_input(email: Email, attachments: list[EmailAttachment], mode: str) -> dict[str, Any]:
    latest_reply = clean_email_body(email)
    conversation_body = email.clean_body or email.text_body or latest_reply
    max_body_chars = max(1000, settings.AI_MAX_INPUT_CHARS // 2)
    attachment_budget = max(1000, settings.AI_MAX_INPUT_CHARS - max_body_chars)
    attachment_items: list[dict[str, Any]] = []
    attachment_chars = 0
    for attachment in attachments:
        extracted_json = attachment.extracted_json
        if (
            isinstance(extracted_json, dict)
            and extracted_json.get("attachment_role") == "engineering_reference"
        ):
            metadata_keys = (
                "file_type",
                "detected_format",
                "attachment_role",
                "business_required",
                "ai_parse_required",
                "blocks_ticket_flow",
                "security_status",
                "parse_skip_reason",
                "detection_warnings",
                "classified_at",
            )
            item = {
                "id": attachment.id,
                "file_name": attachment.file_name,
                "content_type": attachment.content_type,
                "parse_status": attachment.parse_status,
                "classification": {
                    key: extracted_json[key]
                    for key in metadata_keys
                    if key in extracted_json
                },
            }
            item_chars = len(_safe_json(item))
            if attachment_chars + item_chars > attachment_budget:
                break
            attachment_items.append(item)
            attachment_chars += item_chars
            continue
        extracted_json_text = _safe_json(extracted_json) if extracted_json else ""
        if len(extracted_json_text) > 3000:
            extracted_json = {
                "truncated": True,
                "keys": sorted(extracted_json.keys()) if isinstance(extracted_json, dict) else [],
                "preview": extracted_json_text[:2500],
            }
        item = {
            "id": attachment.id,
            "file_name": attachment.file_name,
            "content_type": attachment.content_type,
            "parse_status": attachment.parse_status,
            "extracted_text": _compact_text(attachment.extracted_text, 2500),
            "extracted_json": extracted_json,
            "parse_error": attachment.parse_error,
        }
        item_chars = len(_safe_json(item))
        if attachment_chars + item_chars > attachment_budget:
            break
        attachment_items.append(item)
        attachment_chars += item_chars
    return {
        "mode": mode,
        "email": {
            "id": email.id,
            "subject": email.subject,
            "from_address": email.from_address,
            "to_addresses": email.to_addresses,
            "cc_addresses": email.cc_addresses,
            "sent_at": email.sent_at.isoformat() if email.sent_at else None,
            "received_at": email.received_at.isoformat() if email.received_at else None,
            "in_reply_to": email.in_reply_to,
            "references_header": email.references_header,
            "latest_reply_segment": _compact_text(latest_reply, max_body_chars // 2),
            "conversation_body": _compact_text(conversation_body, max_body_chars),
        },
        "attachments": attachment_items,
    }


def _valid_email(value: str | None) -> bool:
    parsed = parseaddr(value or "")[1]
    return bool(parsed and "@" in parsed and "." in parsed.rsplit("@", 1)[-1])


_PROBLEM_LINE_PATTERN = re.compile(
    r"(?:detected\s+fail|self[ -]?check|\bfail(?:ed|ure|ing)?\b|\bfault\b|"
    r"\berror\b|\babnormal(?:ity)?\b|\bissue\b|\bproblem\b|故障|异常|损坏|不良|失效)",
    re.IGNORECASE,
)
_NEGATED_PROBLEM_PATTERN = re.compile(
    r"\b(?:no|not|without)\s+(?:issue|problem|fault|error|failure)\b|"
    r"(?:无|没有|未发现)(?:故障|异常|问题)",
    re.IGNORECASE,
)


def _problem_description_from_latest_reply(email: Email) -> str | None:
    """Return a short explicit failure statement when the model omitted it."""
    latest_reply = clean_email_body(email)
    candidates: list[str] = []
    for raw_line in latest_reply.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" \t-|;，；")
        if not line or len(line) > 500:
            continue
        if not _PROBLEM_LINE_PATTERN.search(line) or _NEGATED_PROBLEM_PATTERN.search(line):
            continue
        candidates.append(line)
        if len(candidates) == 3:
            break
    return "\n".join(dict.fromkeys(candidates)) or None


def _intent_requires_business_fields(intent_type: str | None) -> bool:
    return intent_type in {"new_repair", "thread_new_repair", "customer_supplement"}


def _structured_attachment_business_data(
    attachments: list[EmailAttachment],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[int]]:
    """Map deterministic attachment parser output to canonical ticket fields."""
    field_aliases = {
        "customer_name": "customer_name",
        "contact_person": "contact_person",
        "contact_email": "contact_email",
        "contact_phone": "contact_phone",
        "phone": "contact_phone",
        "request_date": "request_date",
        "mailing_address": "mailing_address",
        # The legacy repair form labels the customer's own "Mailing Add" as
        # return_address. This alias is accepted only for the deterministic
        # attachment parser; system-calculated repair routes never enter here.
        "return_address": "mailing_address",
        "problem_description": "problem_description",
        "failure_description": "problem_description",
    }
    fields: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    source_ids: list[int] = []
    for attachment in attachments:
        payload = attachment.extracted_json
        if not isinstance(payload, dict) or attachment.parse_status != "parsed":
            continue
        source_ids.append(int(attachment.id))
        attachment_fields = payload.get("extracted_fields")
        if isinstance(attachment_fields, dict):
            for source_name, target_name in field_aliases.items():
                value = attachment_fields.get(source_name)
                if value is not None and str(value).strip() and target_name not in fields:
                    fields[target_name] = value
        attachment_items = payload.get("extracted_items")
        if not isinstance(attachment_items, list):
            continue
        for index, raw_item in enumerate(attachment_items, start=1):
            if not isinstance(raw_item, dict):
                continue
            normalized_item = normalize_repair_item(raw_item, default_line_no=index)
            item = {
                "line_no": normalized_item.get("line_no", index),
                "sn": normalized_item.get("sn"),
                "board_code": (
                    normalized_item.get("board_code")
                    or normalized_item.get("board_model")
                ),
                "board_name": normalized_item.get("board_name"),
                "failure_description": (
                    normalized_item.get("failure_description")
                    or normalized_item.get("failure_information")
                ),
                "failure_information": normalized_item.get("failure_information"),
                "data_info": normalized_item.get("data"),
                "remarks": normalized_item.get("remarks"),
            }
            items.append({key: value for key, value in item.items() if value is not None and str(value).strip()})
    if not fields.get("problem_description"):
        descriptions = [str(item["failure_description"]).strip() for item in items if item.get("failure_description")]
        if descriptions:
            fields["problem_description"] = "\n".join(dict.fromkeys(descriptions))
    return fields, items, source_ids


def _merge_attachment_business_data(
    parsed: AiExtractResponse,
    attachments: list[EmailAttachment],
) -> AiExtractResponse:
    attachment_fields, attachment_items, source_ids = _structured_attachment_business_data(attachments)
    if not source_ids:
        return parsed
    fields = dict(parsed.extracted_fields or {})
    conflicts = dict(parsed.conflict_fields or {})
    confidences = dict(parsed.field_confidences or {})
    for name, value in attachment_fields.items():
        existing = fields.get(name)
        if existing is None or not str(existing).strip():
            fields[name] = value
            confidences[name] = max(float(confidences.get(name) or 0), 0.99)
        elif str(existing).strip().casefold() != str(value).strip().casefold():
            conflicts.setdefault(name, "AI extraction conflicts with deterministic attachment parsing.")

    ai_items = normalize_repair_items(
        dict(item) for item in (parsed.extracted_items or []) if isinstance(item, dict)
    )
    deterministic_items = normalize_repair_items(attachment_items)
    ai_sns = {canonical_sn(item) for item in ai_items if canonical_sn(item)}
    deterministic_sns = {canonical_sn(item) for item in deterministic_items if canonical_sn(item)}
    if ai_sns and deterministic_sns and ai_sns != deterministic_sns:
        conflicts.setdefault("sn", "AI extraction conflicts with deterministic attachment parsing.")

    items_by_sn = {canonical_sn(item): item for item in ai_items if canonical_sn(item)}
    items = list(ai_items)
    for attachment_item in deterministic_items:
        sn = canonical_sn(attachment_item)
        target = items_by_sn.get(sn) if sn else None
        if target is None:
            target = dict(attachment_item)
            items.append(target)
            if sn:
                items_by_sn[sn] = target
            continue
        for name, value in attachment_item.items():
            if (
                (target.get(name) is None or (isinstance(target.get(name), str) and not target.get(name).strip()))
                and value is not None
                and (not isinstance(value, str) or bool(value.strip()))
            ):
                target[name] = value

    evidence = dict(parsed.evidence or {})
    evidence["structured_attachment_source_ids"] = source_ids
    parsed.extracted_fields = fields
    parsed.extracted_items = items
    parsed.conflict_fields = conflicts
    parsed.field_confidences = confidences
    parsed.evidence = evidence
    return parsed


async def _request_date_source(
    session: AsyncSession,
    *,
    email: Email,
    intent_type: str | None,
) -> tuple[Email, RepairTicket | None]:
    if intent_type != "customer_supplement" or not email.thread_id:
        return email, None
    thread = await session.get(EmailThread, email.thread_id)
    ticket = await session.get(RepairTicket, thread.ticket_id) if thread and thread.ticket_id else None
    if ticket and ticket.source_email_id:
        source_email = await session.get(Email, ticket.source_email_id)
        if source_email is not None:
            return source_email, ticket
    source_email = await session.scalar(
        select(Email)
        .where(Email.thread_id == email.thread_id, Email.mail_direction == "inbound")
        .order_by(Email.sent_at.asc(), Email.received_at.asc(), Email.id.asc())
        .limit(1)
    )
    return source_email or email, ticket


def _can_auto_recover_customer_supplement(
    *,
    intent_type: str | None,
    existing_ticket: RepairTicket | None,
    expected_missing_fields: set[str],
    missing: dict[str, Any],
    conflicts: dict[str, Any],
) -> bool:
    """Whether a linked supplement can safely resume automated processing."""
    return (
        intent_type == "customer_supplement"
        and existing_ticket is not None
        and bool(expected_missing_fields)
        and not missing
        and not conflicts
    )


def _apply_request_date_fallback(
    *,
    fields: dict[str, Any],
    evidence: dict[str, Any],
    field_confidences: dict[str, float],
    source_email: Email,
    existing_request_date: Any | None = None,
) -> None:
    if existing_request_date:
        fields["request_date"] = (
            existing_request_date.isoformat()
            if hasattr(existing_request_date, "isoformat")
            else str(existing_request_date)
        )
        evidence.setdefault("derived_fields", {})["request_date"] = {
            "source": "existing_ticket",
            "email_id": source_email.id,
        }
        field_confidences["request_date"] = 1.0
        return
    if fields.get("request_date"):
        return
    source_time = source_email.sent_at or source_email.received_at
    if source_time is None:
        return
    fields["request_date"] = source_time.date().isoformat()
    evidence.setdefault("derived_fields", {})["request_date"] = {
        "source": "email_sent_at" if source_email.sent_at else "email_received_at",
        "email_id": source_email.id,
        "timestamp": source_time.isoformat(),
    }
    field_confidences["request_date"] = 1.0


async def _enrich_ai_quality(
    session: AsyncSession,
    *,
    parsed: AiExtractResponse,
    email: Email,
    attachments: list[EmailAttachment],
) -> AiExtractResponse:
    parsed = _merge_attachment_business_data(parsed, attachments)
    fields = dict(parsed.extracted_fields or {})
    missing = dict(parsed.missing_fields or {})
    conflicts = dict(parsed.conflict_fields or {})
    field_confidences = dict(parsed.field_confidences or {})
    evidence = dict(parsed.evidence or {})
    confidence_reasons = list(parsed.confidence_reasons or [])
    manual_directions: list[str] = []
    existing_ticket: RepairTicket | None = None
    expected_supplement_fields: set[str] = set()

    normalized_items = normalize_repair_items(
        dict(item) for item in (parsed.extracted_items or []) if isinstance(item, dict)
    )
    normalized_items = await _replace_ai_sns_with_known_body_assets(
        session,
        email=email,
        items=normalized_items,
        evidence=evidence,
    )
    parsed.extracted_items = normalized_items
    if fields.get("mailing_address"):
        fields["mailing_address"] = _normalize_customer_mailing_address(
            fields["mailing_address"]
        )
    _apply_deterministic_explicit_return_fields(
        fields=fields,
        email=email,
        evidence=evidence,
        field_confidences=field_confidences,
    )
    _sanitize_customer_return_fields(
        fields=fields,
        email=email,
        evidence=evidence,
        field_confidences=field_confidences,
    )
    if parsed.intent_type == "customer_supplement":
        _apply_deterministic_supplement_fields(
            fields=fields,
            email=email,
            evidence=evidence,
            field_confidences=field_confidences,
        )
    if not fields.get("problem_description"):
        descriptions = [
            str(item.get("failure_description")).strip()
            for item in normalized_items
            if item.get("failure_description") and str(item.get("failure_description")).strip()
        ]
        if descriptions:
            fields["problem_description"] = "\n".join(dict.fromkeys(descriptions))
            field_confidences["problem_description"] = max(
                float(field_confidences.get("problem_description") or 0),
                0.95,
            )
    if _intent_requires_business_fields(parsed.intent_type) and not fields.get("problem_description"):
        deterministic_problem = _problem_description_from_latest_reply(email)
        if deterministic_problem:
            fields["problem_description"] = deterministic_problem
            field_confidences["problem_description"] = max(
                float(field_confidences.get("problem_description") or 0),
                0.9,
            )
            evidence.setdefault("derived_fields", {})["problem_description"] = {
                "source": "explicit_failure_statement_in_latest_reply",
                "line_count": len(deterministic_problem.splitlines()),
            }

    if not fields.get("contact_email") and _valid_email(email.from_address):
        fields["contact_email"] = parseaddr(email.from_address)[1] or email.from_address
        confidence_reasons.append("未抽取到联系邮箱，已使用来信地址作为候选联系邮箱。")

    if not parsed.intent_type or parsed.intent_type == "unknown":
        conflicts.setdefault("intent_type", "邮件类型不明确，需要人工确认是否为新报修、客户补充或无关邮件。")
        manual_directions.append("确认邮件类型和是否需要进入报修流程。")

    if _intent_requires_business_fields(parsed.intent_type):
        if fields.get("contact_email") and not _valid_email(str(fields.get("contact_email"))):
            conflicts.setdefault("contact_email", "联系邮箱格式异常。")

        items = parsed.extracted_items or []
        item_sns = [str(item.get("sn") or "").strip().upper() for item in items if isinstance(item, dict) and item.get("sn")]
        if not item_sns:
            missing.setdefault("sn", "缺少设备 SN，无法校验资产。")
        else:
            invalid_sns: list[str] = []
            resolved_assets: list[SnAsset] = []
            for sn in item_sns:
                asset = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
                if asset is None:
                    invalid_sns.append(f"{sn}: 资产库不存在")
                elif asset.asset_status != "valid":
                    invalid_sns.append(f"{sn}: 状态为 {asset.asset_status}")
                else:
                    resolved_assets.append(asset)
            if invalid_sns:
                conflicts.setdefault("sn", "；".join(invalid_sns))
            elif len(resolved_assets) == len(item_sns):
                assets_by_sn = {
                    sn: asset for sn, asset in zip(item_sns, resolved_assets, strict=True)
                }
                for item in normalized_items:
                    asset = assets_by_sn.get(canonical_sn(item))
                    if asset is None:
                        continue
                    if not item.get("material_code"):
                        item["material_code"] = getattr(asset, "material_code", None)
                    if not item.get("material_name"):
                        item["material_name"] = getattr(asset, "material_name", None)
                customer_names = {
                    str(getattr(asset, "customer_name", "") or "").strip()
                    for asset in resolved_assets
                    if str(getattr(asset, "customer_name", "") or "").strip()
                }
                customer_codes = {
                    str(getattr(asset, "customer_code", "") or "").strip()
                    for asset in resolved_assets
                    if str(getattr(asset, "customer_code", "") or "").strip()
                }
                if len(customer_names) == 1 and not fields.get("customer_name"):
                    fields["customer_name"] = next(iter(customer_names))
                    field_confidences["customer_name"] = 1.0
                    evidence.setdefault("derived_fields", {})["customer_name"] = {
                        "source": "sn_asset_consensus",
                        "sn_count": len(resolved_assets),
                    }
                if len(customer_codes) == 1 and not fields.get("customer_code"):
                    fields["customer_code"] = next(iter(customer_codes))
                    field_confidences["customer_code"] = 1.0
                    evidence.setdefault("derived_fields", {})["customer_code"] = {
                        "source": "sn_asset_consensus",
                        "sn_count": len(resolved_assets),
                    }

    if parsed.intent_type in {"new_repair", "customer_supplement"}:
        source_email, existing_ticket = await _request_date_source(
            session,
            email=email,
            intent_type=parsed.intent_type,
        )
        _apply_request_date_fallback(
            fields=fields,
            evidence=evidence,
            field_confidences=field_confidences,
            source_email=source_email,
            existing_request_date=existing_ticket.request_date if existing_ticket else None,
        )
        if parsed.intent_type == "customer_supplement" and existing_ticket is not None:
            original_missing = set((existing_ticket.missing_fields or {}).keys())
            expected_supplement_fields = original_missing
            # A supplement reply contains quoted history by definition.  When
            # the customer is only answering fields that we explicitly asked
            # for, never let AI re-interpret quoted table columns as new SNs or
            # overwrite the original ticket's item structure.
            existing_items = (
                await session.execute(
                    select(RepairTicketItem)
                    .where(RepairTicketItem.ticket_id == existing_ticket.id)
                    .order_by(RepairTicketItem.line_no.asc(), RepairTicketItem.id.asc())
                )
            ).scalars().all()
            parsed.extracted_items = [
                {
                    "line_no": item.line_no,
                    "sn": item.sn,
                    "material_code": item.material_code,
                    "material_name": item.material_name,
                    "board_code": item.board_code,
                    "board_name": item.board_name,
                    "failure_description": item.failure_description,
                }
                for item in existing_items
            ]
            conflicts.pop("sn", None)
            evidence.setdefault("quality_controls", {})[
                "customer_supplement_item_preservation"
            ] = {
                "allowed": True,
                "reason": "linked_ticket_items_are_authoritative_for_requested_field_supplement",
                "ticket_id": existing_ticket.id,
                "item_count": len(existing_items),
            }
            missing = {
                key: value
                for key, value in missing.items()
                if key in original_missing and not fields.get(key)
            }

    missing = required_missing_for_values(
        intent_type=parsed.intent_type,
        fields=fields,
        items=parsed.extracted_items or [],
        reported_missing=missing,
    )

    if missing:
        manual_directions.append("补齐缺失字段：" + "、".join(sorted(missing.keys())))
    if conflicts:
        manual_directions.append("核对冲突或异常字段：" + "、".join(sorted(conflicts.keys())))

    evidence["confidence_basis"] = {
        "sn_valid": "sn" not in conflicts and "sn" not in missing,
        "email_valid": "contact_email" not in conflicts and "contact_email" not in missing,
        "intent_clear": parsed.intent_type not in {None, "", "unknown"},
        "has_missing_fields": bool(missing),
        "has_conflict_fields": bool(conflicts),
        "threshold": settings.CONFIDENCE_THRESHOLD,
    }
    if confidence_reasons:
        evidence["confidence_reasons"] = confidence_reasons
    if parsed.original_evidence:
        evidence["original_evidence"] = parsed.original_evidence
    auto_recover_supplement = _can_auto_recover_customer_supplement(
        intent_type=parsed.intent_type,
        existing_ticket=existing_ticket,
        expected_missing_fields=expected_supplement_fields,
        missing=missing,
        conflicts=conflicts,
    )
    accepted_return_fields = set(
        (
            evidence.get("derived_fields", {})
            .get("accepted_customer_return_fields", {})
            .get("fields", [])
        )
    )
    explicit_return_context_resolved = (
        parsed.intent_type == "new_repair"
        and not missing
        and not conflicts
        and {"mailing_address", "contact_person", "contact_phone"}.issubset(
            accepted_return_fields
        )
    )
    if explicit_return_context_resolved:
        evidence.pop("manual_review_direction", None)
        evidence.setdefault("quality_controls", {})[
            "explicit_customer_return_context"
        ] = {
            "allowed": True,
            "reason": "all_customer_return_fields_supported_by_explicit_return_context",
        }
        parsed.confidence_score = max(
            float(parsed.confidence_score or 0),
            float(settings.AUTO_APPLY_MIN_CONFIDENCE),
        )
    if (
        parsed.manual_review_direction
        and not auto_recover_supplement
        and not explicit_return_context_resolved
    ):
        manual_directions.insert(0, parsed.manual_review_direction)
    if auto_recover_supplement:
        evidence.pop("manual_review_direction", None)
        evidence.setdefault("quality_controls", {})[
            "customer_supplement_auto_recovery"
        ] = {
            "allowed": True,
            "reason": "linked_ticket_complete_without_conflicts",
            "ticket_id": existing_ticket.id,
            "resolved_field_keys": sorted(expected_supplement_fields),
        }
        parsed.confidence_score = max(
            float(parsed.confidence_score or 0),
            float(settings.AUTO_APPLY_MIN_CONFIDENCE),
        )
    if manual_directions:
        evidence["manual_review_direction"] = "；".join(manual_directions)

    parsed.extracted_fields = fields
    parsed.missing_fields = missing
    parsed.conflict_fields = conflicts
    parsed.field_confidences = field_confidences
    parsed.evidence = evidence
    parsed.confidence_reasons = confidence_reasons
    parsed.manual_review_direction = evidence.get("manual_review_direction")
    return parsed


class MultimodalParseResult(BaseModel):
    extracted_fields: dict[str, Any] = {}
    extracted_items: list[dict[str, Any]] = []
    raw_text: str = ""


def _qwen_vl_configured() -> bool:
    return multimodal_ai_configured()


def _oss_url_from_object(oss_obj: OssObject) -> str:
    from urllib.parse import urlparse

    endpoint_url = oss_obj.endpoint or settings.OSS_ENDPOINT
    parsed = urlparse(endpoint_url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    if netloc.startswith("//"):
        netloc = netloc[2:]
    return f"{scheme}://{oss_obj.bucket}.{netloc}/{oss_obj.object_key}"


async def parse_attachment_multimodal(
    session: AsyncSession,
    attachment: EmailAttachment,
) -> dict[str, Any] | None:
    from app.services.attachment_parser import parse_attachment

    return await parse_attachment(session, attachment)


async def create_ai_parse_candidate(
    session: AsyncSession,
    *,
    email: Email,
    attachments: list[EmailAttachment],
    mode: str,
    ticket_id: int | None = None,
    rule_context: dict[str, Any] | None = None,
    multimodal_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    input_payload = _email_input(email, attachments, mode)
    if rule_context:
        input_payload["rule_context"] = rule_context
    if multimodal_results:
        input_payload["multimodal_results"] = multimodal_results
    messages = [
        {"role": "system", "content": REPAIR_FIELD_EXTRACT.system},
        {
            "role": "user",
            "content": (
                "请输出 JSON，字段为 intent_type, extracted_fields, extracted_items, missing_fields, "
                "conflict_fields, confidence_score, field_confidences, evidence, confidence_reasons, "
                "manual_review_direction, original_evidence。\n"
                f"{_safe_json(input_payload)}"
            ),
        },
    ]
    parsed, ai_log = await _run_ai_json(
        session,
        call_type=mode,
        messages=messages,
        response_model=AiExtractResponse,
        input_payload=input_payload,
        input_summary=f"email_id={email.id}; attachments={len(attachments)}; mode={mode}",
        email_id=email.id,
        ticket_id=ticket_id,
    )
    if not isinstance(parsed, AiExtractResponse) or ai_log is None:
        return None
    locked_intent = normalize_intent(email.intent_type)
    locked_decision = decision_for_intent(locked_intent, reason_code=email.classification_reason_code or "PRECLASSIFICATION_LOCKED")
    # Field extraction is downstream of the authoritative preclassification.
    # Quality rules (required fields and deterministic fallbacks) must use the
    # locked ingress intent, never a legacy intent echoed by the extraction
    # model.
    parsed.intent_type = locked_decision.intent_type
    parsed = await _enrich_ai_quality(session, parsed=parsed, email=email, attachments=attachments)
    parsed.handling_level = locked_decision.handling_level
    parsed.classification_version = email.classification_version or CLASSIFICATION_VERSION
    parsed.classification_reason_code = email.classification_reason_code or locked_decision.reason_code

    parse_result = ParseResult(
        email_id=email.id,
        ticket_id=ticket_id,
        parser_type="ai",
        parser_version=REPAIR_FIELD_EXTRACT.version,
        intent_type=parsed.intent_type,
        handling_level=parsed.handling_level,
        classification_version=parsed.classification_version,
        classification_confidence=parsed.confidence_score,
        classification_reason_code=parsed.classification_reason_code,
        extracted_fields=parsed.extracted_fields,
        extracted_items={"items": parsed.extracted_items},
        missing_fields=parsed.missing_fields,
        conflict_fields=parsed.conflict_fields,
        confidence_score=parsed.confidence_score,
        field_confidences=parsed.field_confidences,
        evidence={
            **parsed.evidence,
            "candidate_intents": parsed.candidate_intents,
            "source_type": "ai",
            "trace_id": ai_log.trace_id,
            "ai_call_log_id": ai_log.id,
            "provider": ai_log.provider_name,
            "model": ai_log.model_name,
            "route_name": ai_log.route_name,
            "fallback_used": ai_log.fallback_used,
            "prompt_version": REPAIR_FIELD_EXTRACT.version,
            "mode": mode,
        },
        apply_status="pending",
    )
    session.add(parse_result)
    await session.flush()
    return {"parse_result": parse_result, "ai_call_log": ai_log}


async def generate_ai_reply_draft(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    related_email: Email | None,
    reply_type: str,
    language: str,
    missing_fields: dict[str, Any] | None,
    template_subject: str,
    template_body: str,
) -> dict[str, Any] | None:
    input_payload = {
        "reply_type": reply_type,
        "language": language,
        "ticket": {
            "id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "current_status_code": ticket.current_status_code,
            "customer_name": ticket.customer_name,
            "contact_person": ticket.contact_person,
            "contact_email": ticket.contact_email,
            "problem_description": _compact_text(ticket.problem_description, 2000),
            "missing_fields": missing_fields,
        },
        "source_email": {
            "id": related_email.id,
            "subject": related_email.subject,
            "from_address": related_email.from_address,
            "latest_reply_segment": _compact_text(related_email.latest_reply_segment or related_email.clean_body, 3000),
        }
        if related_email
        else None,
        "template_draft": {
            "subject": template_subject,
            "body": template_body,
        },
    }
    messages = [
        {"role": "system", "content": REPLY_DRAFT.system},
        {
            "role": "user",
            "content": (
                "请输出 JSON，字段为 subject, body, missing_fields, confidence_score, risk_level, suggestions。\n"
                f"{_safe_json(input_payload)}"
            ),
        },
    ]
    parsed, ai_log = await _run_ai_json(
        session,
        call_type="reply_draft",
        messages=messages,
        response_model=AiReplyDraftResponse,
        input_payload=input_payload,
        input_summary=f"ticket_id={ticket.id}; ticket_no={ticket.ticket_no}; reply_type={reply_type}",
        email_id=related_email.id if related_email else None,
        ticket_id=ticket.id,
    )
    if not isinstance(parsed, AiReplyDraftResponse) or ai_log is None:
        return None
    if not parsed.subject.strip() or not parsed.body.strip() or parsed.confidence_score < 0.5:
        return None
    return {
        "subject": parsed.subject.strip(),
        "body": parsed.body.strip(),
        "missing_fields": parsed.missing_fields or missing_fields,
        "confidence_score": parsed.confidence_score,
        "risk_level": parsed.risk_level,
        "ai_call_log": ai_log,
    }
