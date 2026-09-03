from __future__ import annotations

import hashlib
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmailOutbox, MailDeliveryEvent, ManualReviewTask
from app.services.common import normalize_message_id, utcnow


@dataclass(frozen=True)
class DsnEvidence:
    original_message_id: str | None
    final_recipient: str | None
    action: str | None
    status_code: str | None
    diagnostic_code: str | None
    delivery_status: str


def _header(part: Message, name: str) -> str | None:
    value = part.get(name)
    return str(value).strip() if value else None


def _walk_delivery_blocks(message: Message) -> list[Message]:
    blocks: list[Message] = []
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        payload = part.get_payload()
        if isinstance(payload, list):
            blocks.extend(item for item in payload if isinstance(item, Message))
        else:
            blocks.append(part)
    return blocks


def parse_dsn(raw_eml: bytes) -> DsnEvidence | None:
    message = BytesParser(policy=policy.default).parsebytes(raw_eml)
    sender = parseaddr(str(message.get("From") or ""))[1].lower()
    report_type = str(message.get_param("report-type") or "").lower()
    is_report = message.get_content_type() == "multipart/report" and report_type == "delivery-status"
    if not is_report and "mailer-daemon" not in sender and "postmaster" not in sender:
        return None

    original_message_id = normalize_message_id(
        _header(message, "Original-Message-ID") or _header(message, "X-Original-Message-ID")
    )
    final_recipient = action = status_code = diagnostic_code = None
    for block in _walk_delivery_blocks(message):
        original_message_id = original_message_id or normalize_message_id(
            _header(block, "Original-Message-ID")
        )
        final_recipient = final_recipient or _header(block, "Final-Recipient") or _header(block, "Original-Recipient")
        action = action or (_header(block, "Action") or "").lower() or None
        status_code = status_code or _header(block, "Status")
        diagnostic_code = diagnostic_code or _header(block, "Diagnostic-Code")
    if original_message_id is None:
        for part in message.walk():
            if part.get_content_type() == "message/rfc822":
                payload = part.get_payload()
                nested = payload[0] if isinstance(payload, list) and payload else None
                if isinstance(nested, Message):
                    original_message_id = normalize_message_id(_header(nested, "Message-ID"))
                    if original_message_id:
                        break

    if action == "failed" or (status_code or "").startswith("5"):
        delivery_status = "hard_bounce"
    elif action == "delayed" or (status_code or "").startswith("4"):
        delivery_status = "soft_bounce"
    else:
        delivery_status = "dsn_unknown"
    return DsnEvidence(
        original_message_id=original_message_id,
        final_recipient=final_recipient,
        action=action,
        status_code=status_code,
        diagnostic_code=diagnostic_code,
        delivery_status=delivery_status,
    )


async def persist_dsn_event(
    session: AsyncSession,
    *,
    raw_eml: bytes,
    raw_sha256: str,
) -> MailDeliveryEvent | None:
    evidence = parse_dsn(raw_eml)
    if evidence is None:
        return None
    outbox = None
    if evidence.original_message_id:
        outbox = await session.scalar(
            select(EmailOutbox).where(EmailOutbox.message_id == evidence.original_message_id)
        )
    event_key = hashlib.sha256(
        "|".join(
            [
                raw_sha256,
                evidence.original_message_id or "",
                evidence.final_recipient or "",
                evidence.delivery_status,
                evidence.status_code or "",
            ]
        ).encode("utf-8")
    ).hexdigest()
    existing = await session.scalar(
        select(MailDeliveryEvent).where(MailDeliveryEvent.event_key == event_key)
    )
    if existing is not None:
        return existing
    event = MailDeliveryEvent(
        event_key=event_key,
        outbox_id=outbox.id if outbox else None,
        ticket_id=outbox.ticket_id if outbox else None,
        original_message_id=evidence.original_message_id,
        final_recipient=evidence.final_recipient,
        delivery_status=evidence.delivery_status,
        action=evidence.action,
        smtp_status_code=evidence.status_code,
        diagnostic_code=evidence.diagnostic_code,
        evidence={"raw_eml_sha256": raw_sha256, "correlated": outbox is not None},
        occurred_at=utcnow(),
    )
    session.add(event)
    await session.flush()
    if evidence.delivery_status == "hard_bounce" or outbox is None:
        session.add(
            ManualReviewTask(
                ticket_id=outbox.ticket_id if outbox else None,
                task_type="mail_delivery_hard_bounce" if evidence.delivery_status == "hard_bounce" else "mail_delivery_unmatched",
                priority="high",
                status="pending",
                description="邮件投递永久失败，工单保持关闭并等待人工处理。" if evidence.delivery_status == "hard_bounce" else "收到无法关联到出站邮件的 DSN。",
                trigger_reason=evidence.delivery_status.upper(),
                recovery_stage="delivery_tracking",
                recovery_action="核对收件地址和原始出站 Message-ID；禁止直接重发整封 RMA 邮件。",
            )
        )
    return event
