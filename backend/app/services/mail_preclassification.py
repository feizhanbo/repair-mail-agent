from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
from typing import Any

from app.ai.prompts import MAIL_PRECLASSIFICATION
from app.ai.schemas import (
    ClassificationAttachmentMetadata,
    ClassificationInput,
    ClassificationOutcomeCode,
    ClassificationRuleSignals,
    ClassificationThreadContext,
    MailPreclassificationResponse,
)
from app.config import settings
from app.core.email_classification import (
    CLASSIFICATION_VERSION,
    EmailIntent,
    HandlingLevel,
    decision_for_intent,
)
from app.integrations.ai_provider import AiProviderError
from app.integrations.llm_gateway import LlmTask, invoke_structured
from app.schemas.business import EmailIngestRequest
from app.services.attachment_parser import (
    _extract_csv, _extract_docx, _extract_html, _extract_pdf_text, _extract_prc, _extract_txt, _extract_xlsx,
    render_pdf_pages,
)
from app.services.parser import extract_latest_reply_segment, html_to_text, normalize_email_body
from sqlalchemy.ext.asyncio import AsyncSession


PRECLASSIFICATION_PROMPT_VERSION = MAIL_PRECLASSIFICATION.version


@dataclass(frozen=True)
class MailPreclassificationDecision:
    intent_type: str
    handling_level: str
    confidence: float
    reason_code: str
    candidates: list[dict[str, Any]]
    needs_attachment_content: bool
    evidence: list[dict[str, Any]]
    model_reason_code: str | None = None
    outcome_code: str = str(ClassificationOutcomeCode.CLASSIFIED)
    classification_version: str = CLASSIFICATION_VERSION


def _context(
    payload: EmailIngestRequest,
    *,
    thread_summary: dict[str, Any] | None = None,
    rule_signals: dict[str, Any] | None = None,
) -> ClassificationInput:
    body = normalize_email_body(payload.text_body or html_to_text(payload.html_body))
    latest = extract_latest_reply_segment(body)
    summary = thread_summary or {}
    missing = summary.get("ticket_missing_fields")
    thread = ClassificationThreadContext(
        thread_id=summary.get("thread_id"),
        thread_root_message_id=summary.get("thread_root_message_id"),
        latest_intent=summary.get("latest_intent"),
        latest_handling_level=summary.get("latest_handling_level"),
        ticket_id=summary.get("ticket_id"),
        ticket_category=summary.get("ticket_category"),
        ticket_status=summary.get("ticket_status"),
        ticket_missing_fields=sorted(missing) if isinstance(missing, dict) else list(missing or []),
        known_serial_numbers=list(summary.get("known_serial_numbers") or []),
        rma_status=summary.get("rma_status"),
        has_rma=bool(summary.get("has_rma")),
    )
    signals = ClassificationRuleSignals.model_validate(rule_signals or {})
    return ClassificationInput(
        message_id=payload.message_id,
        subject=payload.subject,
        sender=payload.from_address,
        recipients=list(payload.to_addresses or []),
        latest_message=latest[: settings.MAIL_PRECLASSIFICATION_LATEST_REPLY_CHARS],
        conversation_body=body[: settings.MAIL_PRECLASSIFICATION_BODY_CHARS],
        in_reply_to=payload.in_reply_to,
        references=payload.references_header,
        latest_message_truncated=len(latest) > settings.MAIL_PRECLASSIFICATION_LATEST_REPLY_CHARS,
        conversation_body_truncated=len(body) > settings.MAIL_PRECLASSIFICATION_BODY_CHARS,
        thread_context=thread,
        rule_signals=signals,
        attachments=[
            ClassificationAttachmentMetadata(
                file_name=str(item.get("file_name") or "attachment"),
                content_type=item.get("content_type"),
                file_size=item.get("file_size"),
            )
            for item in payload.attachments
        ],
    )


def transient_attachment_evidence(blobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build bounded in-memory evidence without OSS or ORM persistence."""
    evidence: list[dict[str, Any]] = []
    for blob in blobs[: settings.MAIL_PRECLASSIFICATION_MAX_ATTACHMENTS]:
        content = blob.get("content")
        if not isinstance(content, bytes) or len(content) > settings.MAIL_PRECLASSIFICATION_ATTACHMENT_MAX_BYTES:
            continue
        content_type = str(blob.get("content_type") or "application/octet-stream")
        item: dict[str, Any] = {
            "file_name": blob.get("file_name"),
            "content_type": content_type,
            "file_size": len(content),
        }
        file_name = str(blob.get("file_name") or "").lower()
        try:
            if content_type.startswith("text/") or file_name.endswith(".txt"):
                text = _extract_txt(content)
            elif file_name.endswith(".csv"):
                text = _extract_csv(content)
            elif file_name.endswith(".prc"):
                text = _extract_prc(content)
            elif file_name.endswith((".html", ".htm")):
                text = _extract_html(content)
            elif file_name.endswith(".docx"):
                text = _extract_docx(content)
            elif file_name.endswith(".xlsx"):
                text = _extract_xlsx(content, max_sheets=3, max_rows=100, max_columns=30)
            elif file_name.endswith(".pdf") or content_type == "application/pdf":
                text, page_count = _extract_pdf_text(content, max_pages=5)
                item["pdf_page_count"] = page_count
                if not text:
                    pages, rendered_count = render_pdf_pages(content, max_pages=3)
                    item["data_urls"] = [
                        f"data:image/png;base64,{base64.b64encode(page).decode('ascii')}" for page in pages
                    ]
                    item["rendered_page_count"] = rendered_count
            else:
                text = ""
            if text:
                limit = settings.MAIL_PRECLASSIFICATION_ATTACHMENT_TEXT_CHARS
                item["text"] = text[:limit]
                item["truncated"] = len(text) > limit
            elif content_type.startswith("image/"):
                item["data_url"] = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
            else:
                item["note"] = "bounded binary attachment; no safe transient extractor available"
        except (ValueError, OSError, KeyError) as exc:
            item["note"] = f"transient extraction failed: {exc}"
        evidence.append(item)
    return evidence


async def classify_mail(
    payload: EmailIngestRequest,
    *,
    session: AsyncSession | None = None,
    mail_fetch_record_id: int | None = None,
    thread_summary: dict[str, Any] | None = None,
    attachment_evidence: list[dict[str, Any]] | None = None,
    rule_signals: dict[str, Any] | None = None,
) -> MailPreclassificationDecision:
    context = _context(payload, thread_summary=thread_summary, rule_signals=rule_signals)
    if attachment_evidence:
        by_name = {str(item.get("file_name") or ""): item for item in attachment_evidence}
        enriched: list[ClassificationAttachmentMetadata] = []
        for item in context.attachments:
            evidence_item = by_name.get(item.file_name, {})
            enriched.append(item.model_copy(update={
                "content": evidence_item.get("text"),
                "truncated": bool(evidence_item.get("truncated")),
                "visual_available": bool(evidence_item.get("data_url") or evidence_item.get("data_urls")),
            }))
        context = context.model_copy(update={"attachments": enriched})
    audit_context = context.model_dump(mode="json")
    user_text = context.model_dump_json()
    visual_urls = [
        url
        for item in (attachment_evidence or [])
        for url in ([item["data_url"]] if item.get("data_url") else list(item.get("data_urls") or []))
    ]
    user_content: Any = user_text
    if visual_urls:
        user_content = [
            *[{"type": "image_url", "image_url": {"url": url}} for url in visual_urls],
            {"type": "text", "text": user_text},
        ]
    messages = [
        {
            "role": "system",
            "content": (
                MAIL_PRECLASSIFICATION.system
            ),
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]
    completion = None
    try:
        completion = await invoke_structured(
            task=LlmTask.MAIL_CLASSIFICATION_VISUAL if visual_urls else LlmTask.MAIL_CLASSIFICATION,
            messages=messages,
            response_model=MailPreclassificationResponse,
            temperature=0.0,
        )
        result = completion.parsed
    except AiProviderError as exc:
        failure_outcome = (
            ClassificationOutcomeCode.PRECLASSIFICATION_SCHEMA_FAILED
            if any(code in str(exc).upper() for code in ("OUTPUT_SCHEMA_INVALID", "OUTPUT_NOT_JSON"))
            else ClassificationOutcomeCode.PRECLASSIFICATION_PROVIDER_FAILED
        )
        if session is not None:
            from app.services.ai import persist_ai_log

            await persist_ai_log(
                session,
                trace_id=getattr(exc, "trace_id", "preclassification-failed"),
                call_type="mail_classification",
                input_payload=audit_context,
                request_payload=getattr(exc, "request_payload", None),
                output_payload=getattr(exc, "response_payload", None),
                parsed=None,
                latency_ms=getattr(exc, "latency_ms", None),
                input_summary=str(payload.subject or "")[:1000],
                output_summary=None,
                mail_fetch_record_id=mail_fetch_record_id,
                provider_name=str(getattr(exc, "route_name", "unknown")),
                model_name=str(getattr(exc, "model_name", "unknown")),
                prompt_version=PRECLASSIFICATION_PROMPT_VERSION,
                prompt_hash=MAIL_PRECLASSIFICATION.content_hash,
                schema_version=MAIL_PRECLASSIFICATION.schema_version,
                parser_version=MAIL_PRECLASSIFICATION.parser_version,
                structured_output_method=getattr(exc, "structured_output_method", None),
                route_name=getattr(exc, "route_name", None),
                route_attempt=int(getattr(exc, "route_attempt", 1)),
                fallback_used=int(getattr(exc, "route_attempt", 1)) > 1,
                error_message=str(exc),
            )
        return unknown_decision(str(failure_outcome))

    if session is not None:
        from app.services.ai import persist_ai_log

        await persist_ai_log(
            session,
            trace_id=completion.trace_id,
            call_type="mail_classification",
            input_payload=audit_context,
            request_payload=completion.request_payload,
            output_payload=completion.response_payload,
            parsed=result,
            latency_ms=completion.latency_ms,
            input_summary=str(payload.subject or "")[:1000],
            output_summary=f"{result.intent}:{result.confidence}",
            mail_fetch_record_id=mail_fetch_record_id,
            provider_name=getattr(completion, "provider_name", None) or "unknown",
            model_name=getattr(completion, "model_name", None) or "unknown",
            prompt_version=PRECLASSIFICATION_PROMPT_VERSION,
            prompt_hash=MAIL_PRECLASSIFICATION.content_hash,
            schema_version=MAIL_PRECLASSIFICATION.schema_version,
            parser_version=MAIL_PRECLASSIFICATION.parser_version,
            structured_output_method=completion.structured_output_method,
            route_name=getattr(completion, "route_name", None),
            route_attempt=int(getattr(completion, "route_attempt", 1)),
            fallback_used=bool(getattr(completion, "fallback_used", False)),
        )

    async def finalize(decision: MailPreclassificationDecision) -> MailPreclassificationDecision:
        if session is not None:
            from app.services.ai import persist_ai_normalized_result

            await persist_ai_normalized_result(
                trace_id=completion.trace_id,
                stage="mail_classification",
                normalized_result=asdict(decision),
                prompt_version=MAIL_PRECLASSIFICATION.version,
                schema_version=MAIL_PRECLASSIFICATION.schema_version,
                parser_version=MAIL_PRECLASSIFICATION.parser_version,
            )
        return decision

    # 链路 smoke 测试开关：非空时强制最终 intent，需在低置信度/候选冲突判定之前，
    # 否则 Qwen 等模型返回 low-confidence 时会先短路为 unknown，导致开关失效。
    forced = settings.MAIL_INTENT_FORCE.strip()
    if forced:
        canonical = decision_for_intent(forced, reason_code=f"SMOKE_FORCE:{forced}")
        return await finalize(MailPreclassificationDecision(
            intent_type=canonical.intent_type,
            handling_level=canonical.handling_level,
            confidence=result.confidence,
            reason_code=canonical.reason_code,
            candidates=[candidate.model_dump() for candidate in result.candidates],
            needs_attachment_content=result.needs_attachment_content,
            evidence=[item.model_dump(mode="json") for item in result.evidence],
            model_reason_code=str(result.reason_code),
            outcome_code=str(ClassificationOutcomeCode.SMOKE_FORCE),
        ))
    canonical = decision_for_intent(result.intent, reason_code=str(result.reason_code))
    below_threshold = result.confidence < settings.MAIL_PRECLASSIFICATION_MIN_CONFIDENCE
    conflicting = bool(result.candidates) and result.candidates[0].intent != canonical.intent_type
    if below_threshold or conflicting:
        reason = "PRECLASSIFICATION_LOW_CONFIDENCE" if below_threshold else "PRECLASSIFICATION_INTENT_CONFLICT"
        return await finalize(unknown_decision(
            reason,
            confidence=result.confidence,
            evidence=[item.model_dump(mode="json") for item in result.evidence],
            needs_attachment_content=result.needs_attachment_content,
            candidates=[candidate.model_dump() for candidate in result.candidates],
            model_reason_code=str(result.reason_code),
        ))
    return await finalize(MailPreclassificationDecision(
        intent_type=canonical.intent_type,
        handling_level=canonical.handling_level,
        confidence=result.confidence,
        reason_code=canonical.reason_code,
        candidates=[candidate.model_dump() for candidate in result.candidates],
        needs_attachment_content=result.needs_attachment_content,
        evidence=[item.model_dump(mode="json") for item in result.evidence],
        model_reason_code=str(result.reason_code),
        outcome_code=str(ClassificationOutcomeCode.CLASSIFIED),
    ))


def unknown_decision(
    reason_code: str,
    *,
    confidence: float = 0.0,
    evidence: list[dict[str, Any]] | None = None,
    needs_attachment_content: bool = False,
    candidates: list[dict[str, Any]] | None = None,
    model_reason_code: str | None = None,
) -> MailPreclassificationDecision:
    outcome = reason_code if reason_code in {item.value for item in ClassificationOutcomeCode} else str(ClassificationOutcomeCode.PRECLASSIFICATION_PROVIDER_FAILED)
    return MailPreclassificationDecision(
        intent_type=str(EmailIntent.UNKNOWN),
        handling_level=str(HandlingLevel.UNKNOWN),
        confidence=confidence,
        reason_code=reason_code,
        candidates=candidates or [],
        needs_attachment_content=needs_attachment_content,
        evidence=evidence or [],
        model_reason_code=model_reason_code,
        outcome_code=outcome,
    )
