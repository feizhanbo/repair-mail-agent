from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Email
from app.models.mail_fetch import MailFetchRecord
from app.schemas.business import EmailIngestRequest
from app.services.common import address_domain, normalize_message_id
from app.services.common import utcnow
from app.services.parser import RuleAnalysisResult, analyze_email_rules


@dataclass(frozen=True)
class MailPrecheckResult:
    accepted: bool
    status: str
    reason: str
    message_id: str | None = None
    intent_type: str | None = None
    confidence: float | None = None
    duplicate_email_id: int | None = None
    duplicate_fetch_record_id: int | None = None
    rule_analysis: RuleAnalysisResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "reason": self.reason,
            "message_id": self.message_id,
            "intent_type": self.intent_type,
            "confidence": self.confidence,
            "duplicate_email_id": self.duplicate_email_id,
            "duplicate_fetch_record_id": self.duplicate_fetch_record_id,
            "rule_analysis": self.rule_analysis.summary() if self.rule_analysis else None,
        }


def _payload_as_lightweight_email(payload: EmailIngestRequest, message_id: str) -> Email:
    email = Email(
        mail_direction="inbound",
        mailbox_account=payload.mailbox_account,
        folder_name=payload.folder_name,
        imap_uid=payload.imap_uid,
        message_id=message_id,
        in_reply_to=payload.in_reply_to,
        references_header=payload.references_header,
        from_address=payload.from_address,
        from_domain=address_domain(payload.from_address),
        to_addresses=payload.to_addresses,
        cc_addresses=payload.cc_addresses,
        subject=payload.subject,
        text_body=payload.text_body,
        html_body=payload.html_body,
        clean_body=payload.text_body,
        latest_reply_segment=payload.text_body,
        parse_status="pending",
    )
    return email


async def precheck_imap_uid(
    session: AsyncSession,
    *,
    mailbox_account: str,
    folder_name: str,
    imap_uid: str,
) -> MailPrecheckResult | None:
    existing = await session.scalar(
        select(MailFetchRecord).where(
            MailFetchRecord.mailbox_account == mailbox_account,
            MailFetchRecord.folder_name == folder_name,
            MailFetchRecord.imap_uid == imap_uid,
        )
    )
    if existing is None:
        return None
    if existing.fetch_status in {"failed", "retry_wait"}:
        retry_due = existing.next_retry_at is None or existing.next_retry_at <= utcnow()
        if retry_due and existing.attempt_count < settings.IMAP_MAX_RETRIES:
            return None
        return MailPrecheckResult(
            accepted=False,
            status="retry_wait_skipped",
            reason="IMAP UID retry is not due or exhausted.",
            message_id=existing.message_id,
            duplicate_email_id=existing.email_id,
            duplicate_fetch_record_id=existing.id,
        )
    return MailPrecheckResult(
        accepted=False,
        status="duplicate_uid_skipped",
        reason="IMAP UID already processed.",
        message_id=existing.message_id,
        duplicate_email_id=existing.email_id,
        duplicate_fetch_record_id=existing.id,
    )


async def precheck_email_payload(
    session: AsyncSession,
    payload: EmailIngestRequest,
) -> MailPrecheckResult:
    message_id = normalize_message_id(payload.message_id, fallback_hash=payload.raw_eml_sha256)
    payload.message_id = message_id

    duplicate_predicates = [Email.message_id == message_id]
    if payload.raw_eml_sha256:
        duplicate_predicates.append(Email.source_content_sha256 == payload.raw_eml_sha256)
    duplicate = await session.scalar(select(Email).where(or_(*duplicate_predicates)))
    if duplicate is not None:
        return MailPrecheckResult(
            accepted=False,
            status="duplicate_message_skipped",
            reason="Message-ID already ingested.",
            message_id=message_id,
            duplicate_email_id=duplicate.id,
        )

    email = _payload_as_lightweight_email(payload, message_id)
    analysis = analyze_email_rules(email)
    has_valid_attachments = bool(payload.attachments)
    has_business_evidence = bool(analysis.items)
    if (
        analysis.intent_type == "irrelevant"
        and analysis.classification_confidence >= settings.MAIL_PRECHECK_IRRELEVANT_MIN_CONFIDENCE
        and not has_valid_attachments
        and not has_business_evidence
        and not payload.in_reply_to
        and not payload.references_header
    ):
        return MailPrecheckResult(
            accepted=False,
            status="irrelevant_skipped",
            reason=analysis.classification_reason,
            message_id=message_id,
            intent_type=analysis.intent_type,
            confidence=analysis.classification_confidence,
            rule_analysis=analysis,
        )

    return MailPrecheckResult(
        accepted=True,
        status="accepted",
        reason=analysis.classification_reason,
        message_id=message_id,
        intent_type=analysis.intent_type,
        confidence=analysis.classification_confidence,
        rule_analysis=analysis,
    )
