from __future__ import annotations

from dataclasses import dataclass
import base64
from typing import Any

from pydantic import BaseModel, Field

from app.ai.prompts import MAIL_PRECLASSIFICATION
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


class IntentCandidate(BaseModel):
    intent: EmailIntent
    confidence: float = Field(ge=0, le=1)


class MailPreclassificationResponse(BaseModel):
    intent: EmailIntent
    handling_level: HandlingLevel | None = None
    confidence: float = Field(ge=0, le=1)
    candidates: list[IntentCandidate] = Field(default_factory=list)
    reason_code: str = Field(min_length=1, max_length=100)
    needs_attachment_content: bool = False
    evidence: list[str] = Field(default_factory=list)

@dataclass(frozen=True)
class MailPreclassificationDecision:
    intent_type: str
    handling_level: str
    confidence: float
    reason_code: str
    candidates: list[dict[str, Any]]
    needs_attachment_content: bool
    evidence: list[str]
    classification_version: str = PRECLASSIFICATION_PROMPT_VERSION


def _context(payload: EmailIngestRequest, *, thread_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    body = normalize_email_body(payload.text_body or html_to_text(payload.html_body))
    latest = extract_latest_reply_segment(body)
    return {
        "subject": payload.subject,
        "from": payload.from_address,
        "to": payload.to_addresses,
        "latest_reply_segment": latest[: settings.MAIL_PRECLASSIFICATION_LATEST_REPLY_CHARS],
        "body": body[: settings.MAIL_PRECLASSIFICATION_BODY_CHARS],
        "truncated": {
            "latest_reply_segment": len(latest) > settings.MAIL_PRECLASSIFICATION_LATEST_REPLY_CHARS,
            "body": len(body) > settings.MAIL_PRECLASSIFICATION_BODY_CHARS,
        },
        "message_id": payload.message_id,
        "in_reply_to": payload.in_reply_to,
        "references": payload.references_header,
        "thread_summary": thread_summary or {},
        "attachments": [
            {
                "file_name": item.get("file_name"),
                "content_type": item.get("content_type"),
                "file_size": item.get("file_size"),
            }
            for item in payload.attachments
        ],
    }


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
) -> MailPreclassificationDecision:
    context = _context(payload, thread_summary=thread_summary)
    if attachment_evidence:
        context["attachment_evidence"] = attachment_evidence
    audit_context = dict(context)
    if attachment_evidence:
        audit_context["attachment_evidence"] = [
            {key: value for key, value in item.items() if key not in {"data_url", "data_urls"}}
            | ({"visual_bytes_in_memory": True} if item.get("data_url") or item.get("data_urls") else {})
            for item in attachment_evidence
        ]
    user_text = (
        f"prompt_version={PRECLASSIFICATION_PROMPT_VERSION}\n"
        "请返回 intent, handling_level, confidence, candidates, reason_code, "
        f"needs_attachment_content, evidence。上下文：{context}"
    )
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
            task=LlmTask.ATTACHMENT_VISUAL_PARSE if visual_urls else LlmTask.MAIL_CLASSIFICATION,
            messages=messages,
            response_model=MailPreclassificationResponse,
            temperature=0.0,
        )
        result = completion.parsed
    except AiProviderError as exc:
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
                route_name=getattr(exc, "route_name", None),
                route_attempt=int(getattr(exc, "route_attempt", 1)),
                fallback_used=int(getattr(exc, "route_attempt", 1)) > 1,
                error_message=str(exc),
            )
        return unknown_decision("PRECLASSIFICATION_PROVIDER_FAILED")

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
            route_name=getattr(completion, "route_name", None),
            route_attempt=int(getattr(completion, "route_attempt", 1)),
            fallback_used=bool(getattr(completion, "fallback_used", False)),
        )

    canonical = decision_for_intent(result.intent, reason_code=result.reason_code)
    below_threshold = result.confidence < settings.MAIL_PRECLASSIFICATION_MIN_CONFIDENCE
    conflicting = bool(result.candidates) and result.candidates[0].intent != canonical.intent_type
    if below_threshold or conflicting:
        reason = "PRECLASSIFICATION_LOW_CONFIDENCE" if below_threshold else "PRECLASSIFICATION_INTENT_CONFLICT"
        return unknown_decision(
            reason,
            confidence=result.confidence,
            evidence=result.evidence,
            needs_attachment_content=result.needs_attachment_content,
            candidates=[candidate.model_dump() for candidate in result.candidates],
        )
    return MailPreclassificationDecision(
        intent_type=canonical.intent_type,
        handling_level=canonical.handling_level,
        confidence=result.confidence,
        reason_code=result.reason_code,
        candidates=[candidate.model_dump() for candidate in result.candidates],
        needs_attachment_content=result.needs_attachment_content,
        evidence=result.evidence,
    )


def unknown_decision(
    reason_code: str,
    *,
    confidence: float = 0.0,
    evidence: list[str] | None = None,
    needs_attachment_content: bool = False,
    candidates: list[dict[str, Any]] | None = None,
) -> MailPreclassificationDecision:
    return MailPreclassificationDecision(
        intent_type=str(EmailIntent.UNKNOWN),
        handling_level=str(HandlingLevel.UNKNOWN),
        confidence=confidence,
        reason_code=reason_code,
        candidates=candidates or [],
        needs_attachment_content=needs_attachment_content,
        evidence=evidence or [],
    )
