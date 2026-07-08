from __future__ import annotations

import json
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.ai_provider import AiExtractResponse, AiProviderError, AiReplyDraftResponse, DeepSeekProvider
from app.models import AiCallLog, Email, EmailAttachment, ParseResult, RepairTicket, SnAsset
from app.services.common import sha256_text, to_plain, utcnow
from app.services.parser import clean_email_body

AI_EXTRACT_SYSTEM_PROMPT = """
你是邮件报修自动化系统的结构化解析助手。你只能输出 JSON 对象。
根据输入邮件和附件提取报修意图、工单字段、报修明细、缺失字段、冲突字段、字段置信度和证据片段。
不要编造不存在的信息；不确定字段放入 missing_fields 或 conflict_fields。
""".strip()

AI_EXTRACT_SYSTEM_PROMPT = """
你是邮件报修系统的结构化解析助手，只能输出 JSON 对象。
所有邮件都必须由你判断最终类型；规则解析只作为候选上下文，不能直接决定结果。
请输出 intent_type, extracted_fields, extracted_items, missing_fields, conflict_fields, confidence_score,
field_confidences, evidence, confidence_reasons, manual_review_direction, original_evidence。
置信度必须给出依据：SN 是否有效、邮箱/电话是否正常、字段是否冲突、邮件类型是否准确、正文是否完整、是否有异常。
如果需要人工处理，manual_review_direction 要明确说明人工需要核对什么，并在 original_evidence 放入原始邮件片段依据。
不要编造不存在的信息；不确定字段放入 missing_fields 或 conflict_fields。
""".strip()

AI_REPLY_SYSTEM_PROMPT = """
你是邮件报修自动化系统的中文客服助理。你只能输出 JSON 对象。
根据工单缺失字段和模板草稿生成更自然的追问草稿。草稿只能用于人工审核，不代表已发送。
语气礼貌、简洁，避免承诺维修结果，不要加入输入中不存在的客户信息。
""".strip()


def ai_configured() -> bool:
    return settings.AI_PROVIDER.lower() == "deepseek" and bool(settings.AI_API_KEY)


def _provider() -> DeepSeekProvider:
    return DeepSeekProvider(
        api_key=settings.AI_API_KEY,
        base_url=settings.AI_BASE_URL,
        model=settings.AI_MODEL,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
    )


def _compact_text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    normalized = value.strip()
    return normalized[:limit]


def _safe_json(value: Any) -> str:
    return json.dumps(to_plain(value), ensure_ascii=False, default=str)


def _write_jsonl(record: dict[str, Any]) -> tuple[str, int, str]:
    now = utcnow()
    log_dir = Path("logs") / "ai" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
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
    if call_type in {"field_extract", "classification_and_extract"}:
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
            "subject": data.get("subject"),
            "body_chars": len(body),
            "risk_level": data.get("risk_level"),
            "missing_field_keys": sorted((data.get("missing_fields") or {}).keys()),
            "suggestions": data.get("suggestions") or [],
        }
    return data


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


async def _persist_ai_log(
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
    error_message: str | None = None,
) -> AiCallLog:
    record = {
        "trace_id": trace_id,
        "call_type": call_type,
        "prompt_version": settings.AI_PROMPT_VERSION,
        "provider": settings.AI_PROVIDER,
        "model": settings.AI_MODEL,
        "input": input_payload,
        "request": request_payload,
        "output": output_payload,
        "parsed": parsed.model_dump() if parsed else None,
        "latency_ms": latency_ms,
        "status": _status_for(parsed, error_message),
        "error_message": error_message,
        "created_at": utcnow().isoformat(),
    }
    log_file_path, line_no, record_hash = _write_jsonl(record)
    ai_log = AiCallLog(
        trace_id=trace_id,
        email_id=email_id,
        ticket_id=ticket_id,
        call_type=call_type,
        provider_name=settings.AI_PROVIDER,
        model_name=settings.AI_MODEL,
        prompt_version=settings.AI_PROMPT_VERSION,
        input_summary=input_summary[:1000],
        output_summary=(output_summary or "")[:1000] or None,
        parsed_key_result=_key_result(call_type, parsed),
        confidence_score=_confidence(parsed),
        latency_ms=latency_ms,
        status=_status_for(parsed, error_message),
        error_message=error_message,
        log_file_path=log_file_path,
        log_line_no=line_no,
        log_record_hash=record_hash,
    )
    session.add(ai_log)
    await session.flush()
    return ai_log


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
    if not ai_configured():
        return None, None
    try:
        completion = await _provider().chat_json(messages=messages, response_model=response_model)
    except AiProviderError as exc:
        trace_id = sha256_text(f"{call_type}:{utcnow().isoformat()}")[:32]
        ai_log = await _persist_ai_log(
            session,
            trace_id=trace_id,
            call_type=call_type,
            input_payload=input_payload,
            request_payload={"model": settings.AI_MODEL, "messages": messages, "response_format": {"type": "json_object"}},
            output_payload=None,
            parsed=None,
            latency_ms=None,
            input_summary=input_summary,
            output_summary=None,
            email_id=email_id,
            ticket_id=ticket_id,
            error_message=str(exc),
        )
        return None, ai_log

    parsed = completion.parsed
    output_summary = _summarize_output(call_type, parsed)
    ai_log = await _persist_ai_log(
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
        return f"subject={parsed.subject}; confidence={parsed.confidence_score}; risk={parsed.risk_level}"
    return f"{call_type} completed"


def _email_input(email: Email, attachments: list[EmailAttachment], mode: str) -> dict[str, Any]:
    body = clean_email_body(email)
    max_body_chars = max(1000, settings.AI_MAX_INPUT_CHARS // 2)
    attachment_budget = max(1000, settings.AI_MAX_INPUT_CHARS - max_body_chars)
    attachment_items: list[dict[str, Any]] = []
    for attachment in attachments:
        if len(_safe_json(attachment_items)) >= attachment_budget:
            break
        attachment_items.append(
            {
                "id": attachment.id,
                "file_name": attachment.file_name,
                "content_type": attachment.content_type,
                "parse_status": attachment.parse_status,
                "extracted_text": _compact_text(attachment.extracted_text, 2500),
                "extracted_json": attachment.extracted_json,
                "parse_error": attachment.parse_error,
            }
        )
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
            "clean_body": _compact_text(body, max_body_chars),
        },
        "attachments": attachment_items,
    }


def _valid_email(value: str | None) -> bool:
    parsed = parseaddr(value or "")[1]
    return bool(parsed and "@" in parsed and "." in parsed.rsplit("@", 1)[-1])


def _valid_phone(value: str | None) -> bool:
    if not value:
        return True
    digits = re.sub(r"\D", "", value)
    return 6 <= len(digits) <= 20


def _intent_requires_business_fields(intent_type: str | None) -> bool:
    return intent_type in {"new_repair", "customer_reply", "internal_forward", "unknown"}


async def _enrich_ai_quality(
    session: AsyncSession,
    *,
    parsed: AiExtractResponse,
    email: Email,
) -> AiExtractResponse:
    fields = dict(parsed.extracted_fields or {})
    missing = dict(parsed.missing_fields or {})
    conflicts = dict(parsed.conflict_fields or {})
    evidence = dict(parsed.evidence or {})
    confidence_reasons = list(parsed.confidence_reasons or [])
    manual_directions: list[str] = []

    if not fields.get("contact_email") and _valid_email(email.from_address):
        fields["contact_email"] = parseaddr(email.from_address)[1] or email.from_address
        confidence_reasons.append("未抽取到联系邮箱，已使用来信地址作为候选联系邮箱。")

    if not parsed.intent_type or parsed.intent_type == "unknown":
        conflicts.setdefault("intent_type", "邮件类型不明确，需要人工确认是否为新报修、客户补充或无关邮件。")
        manual_directions.append("确认邮件类型和是否需要进入报修流程。")

    if _intent_requires_business_fields(parsed.intent_type):
        if not fields.get("contact_email"):
            missing.setdefault("contact_email", "缺少可用于回复客户的邮箱地址。")
        elif not _valid_email(str(fields.get("contact_email"))):
            conflicts.setdefault("contact_email", "联系邮箱格式异常。")
        if fields.get("contact_phone") and not _valid_phone(str(fields.get("contact_phone"))):
            conflicts.setdefault("contact_phone", "联系电话格式异常。")
        if not fields.get("problem_description"):
            missing.setdefault("problem_description", "缺少明确的故障描述。")

        items = parsed.extracted_items or []
        item_sns = [str(item.get("sn") or "").strip().upper() for item in items if isinstance(item, dict) and item.get("sn")]
        if not item_sns:
            missing.setdefault("sn", "缺少设备 SN，无法校验资产。")
        else:
            invalid_sns: list[str] = []
            for sn in item_sns:
                asset = await session.scalar(select(SnAsset).where(SnAsset.sn == sn))
                if asset is None:
                    invalid_sns.append(f"{sn}: 资产库不存在")
                elif asset.asset_status != "valid":
                    invalid_sns.append(f"{sn}: 状态为 {asset.asset_status}")
            if invalid_sns:
                conflicts.setdefault("sn", "；".join(invalid_sns))

    if missing:
        manual_directions.append("补齐缺失字段：" + "、".join(sorted(missing.keys())))
    if conflicts:
        manual_directions.append("核对冲突或异常字段：" + "、".join(sorted(conflicts.keys())))

    evidence["confidence_basis"] = {
        "sn_valid": "sn" not in conflicts and "sn" not in missing,
        "email_valid": "contact_email" not in conflicts and "contact_email" not in missing,
        "phone_valid": "contact_phone" not in conflicts,
        "intent_clear": parsed.intent_type not in {None, "", "unknown"},
        "has_missing_fields": bool(missing),
        "has_conflict_fields": bool(conflicts),
        "threshold": settings.CONFIDENCE_THRESHOLD,
    }
    if confidence_reasons:
        evidence["confidence_reasons"] = confidence_reasons
    if parsed.original_evidence:
        evidence["original_evidence"] = parsed.original_evidence
    if parsed.manual_review_direction:
        manual_directions.insert(0, parsed.manual_review_direction)
    if manual_directions:
        evidence["manual_review_direction"] = "；".join(manual_directions)

    parsed.extracted_fields = fields
    parsed.missing_fields = missing
    parsed.conflict_fields = conflicts
    parsed.evidence = evidence
    parsed.confidence_reasons = confidence_reasons
    parsed.manual_review_direction = evidence.get("manual_review_direction")
    return parsed


async def create_ai_parse_candidate(
    session: AsyncSession,
    *,
    email: Email,
    attachments: list[EmailAttachment],
    mode: str,
    ticket_id: int | None = None,
    rule_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    input_payload = _email_input(email, attachments, mode)
    if rule_context:
        input_payload["rule_context"] = rule_context
    messages = [
        {"role": "system", "content": AI_EXTRACT_SYSTEM_PROMPT},
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
        input_summary=f"email_id={email.id}; subject={email.subject or '-'}; attachments={len(attachments)}; mode={mode}",
        email_id=email.id,
        ticket_id=ticket_id,
    )
    if not isinstance(parsed, AiExtractResponse) or ai_log is None:
        return None
    parsed = await _enrich_ai_quality(session, parsed=parsed, email=email)

    parse_result = ParseResult(
        email_id=email.id,
        ticket_id=ticket_id,
        parser_type="ai",
        parser_version=settings.AI_PROMPT_VERSION,
        intent_type=parsed.intent_type,
        extracted_fields=parsed.extracted_fields,
        extracted_items={"items": parsed.extracted_items},
        missing_fields=parsed.missing_fields,
        conflict_fields=parsed.conflict_fields,
        confidence_score=parsed.confidence_score,
        field_confidences=parsed.field_confidences,
        evidence={
            **parsed.evidence,
            "source_type": "ai",
            "trace_id": ai_log.trace_id,
            "ai_call_log_id": ai_log.id,
            "provider": settings.AI_PROVIDER,
            "model": settings.AI_MODEL,
            "prompt_version": settings.AI_PROMPT_VERSION,
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
        {"role": "system", "content": AI_REPLY_SYSTEM_PROMPT},
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
        call_type="generate_reply_draft",
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
