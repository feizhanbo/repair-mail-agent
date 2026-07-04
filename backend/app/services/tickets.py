from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    BoardCard,
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    FieldAuditLog,
    ManualReviewTask,
    ParseResult,
    RepairTicket,
    RepairTicketItem,
    ReplyRecord,
    SnAsset,
    SnValidationResult,
    TicketStatusLog,
)
from app.services.audit import log_operation
from app.services.common import model_to_dict, paginate_scalars, to_plain, utcnow
from app.services.workflow import transition_ticket

TICKET_FIELDS = (
    "id",
    "ticket_no",
    "current_status_code",
    "source_email_id",
    "thread_id",
    "customer_code",
    "customer_name",
    "contact_person",
    "contact_phone",
    "contact_email",
    "request_date",
    "mailing_address",
    "problem_description",
    "accessories",
    "missing_fields",
    "conflict_fields",
    "followup_count",
    "max_followup_count",
    "confidence_score",
    "assigned_user_id",
    "manual_locked",
    "version",
    "created_at",
    "updated_at",
)

TICKET_WRITE_FIELDS = {
    "customer_code",
    "customer_name",
    "contact_person",
    "contact_phone",
    "contact_email",
    "request_date",
    "mailing_address",
    "problem_description",
    "accessories",
    "missing_fields",
    "conflict_fields",
    "confidence_score",
    "assigned_user_id",
    "manual_locked",
}

ITEM_FIELDS = (
    "id",
    "ticket_id",
    "line_no",
    "material_code",
    "material_name",
    "sn",
    "sn_asset_id",
    "quantity",
    "failure_description",
    "failure_information",
    "data_info",
    "remarks",
    "accessories",
    "validation_status",
    "validation_message",
    "manual_locked",
    "created_at",
    "updated_at",
)

ITEM_WRITE_FIELDS = {
    "line_no",
    "material_code",
    "material_name",
    "sn",
    "quantity",
    "failure_description",
    "failure_information",
    "data_info",
    "remarks",
    "accessories",
    "manual_locked",
}

EMAIL_FIELDS = (
    "id",
    "thread_id",
    "mail_direction",
    "mailbox_account",
    "folder_name",
    "imap_uid",
    "message_id",
    "in_reply_to",
    "from_address",
    "from_domain",
    "to_addresses",
    "cc_addresses",
    "subject",
    "normalized_subject",
    "sent_at",
    "received_at",
    "parse_status",
    "intent_type",
    "duplicate_of_email_id",
    "error_message",
    "created_at",
    "updated_at",
)


def serialize_ticket(ticket: RepairTicket) -> dict[str, Any]:
    return model_to_dict(ticket, TICKET_FIELDS)


def serialize_item(item: RepairTicketItem) -> dict[str, Any]:
    return model_to_dict(item, ITEM_FIELDS)


def serialize_email(email: Email) -> dict[str, Any]:
    data = model_to_dict(email, EMAIL_FIELDS)
    data["clean_body"] = email.clean_body
    data["latest_reply_segment"] = email.latest_reply_segment
    return data


def serialize_parse_result(parse_result: ParseResult) -> dict[str, Any]:
    return model_to_dict(
        parse_result,
        (
            "id",
            "email_id",
            "source_attachment_id",
            "ticket_id",
            "parser_type",
            "parser_version",
            "intent_type",
            "extracted_fields",
            "extracted_items",
            "missing_fields",
            "conflict_fields",
            "confidence_score",
            "field_confidences",
            "evidence",
            "accepted",
            "accepted_by_user_id",
            "accepted_at",
            "error_message",
            "created_at",
        ),
    )


async def get_ticket(session: AsyncSession, ticket_id: int) -> RepairTicket:
    ticket = await session.get(RepairTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TICKET_NOT_FOUND")
    return ticket


async def list_tickets(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    status_code: str | None = None,
    keyword: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = select(RepairTicket)
    if status_code:
        statement = statement.where(RepairTicket.current_status_code == status_code)
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(
            or_(
                RepairTicket.ticket_no.like(like),
                RepairTicket.customer_code.like(like),
                RepairTicket.customer_name.like(like),
                RepairTicket.contact_email.like(like),
                RepairTicket.problem_description.like(like),
            )
        )
    statement = statement.order_by(RepairTicket.updated_at.desc(), RepairTicket.id.desc())
    tickets, total = await paginate_scalars(session, statement, page, page_size)
    return [serialize_ticket(ticket) for ticket in tickets], total


async def get_ticket_detail(session: AsyncSession, ticket_id: int) -> dict[str, Any]:
    ticket = await get_ticket(session, ticket_id)
    items = (
        await session.execute(select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id).order_by(RepairTicketItem.line_no))
    ).scalars().all()
    parse_results = (
        await session.execute(select(ParseResult).where(ParseResult.ticket_id == ticket.id).order_by(ParseResult.created_at.desc()))
    ).scalars().all()
    validations = (
        await session.execute(select(SnValidationResult).where(SnValidationResult.ticket_id == ticket.id).order_by(SnValidationResult.checked_at.desc()))
    ).scalars().all()
    tasks = (
        await session.execute(select(ManualReviewTask).where(ManualReviewTask.ticket_id == ticket.id).order_by(ManualReviewTask.created_at.desc()))
    ).scalars().all()
    replies = (
        await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id == ticket.id).order_by(ReplyRecord.created_at.desc()))
    ).scalars().all()
    status_logs = (
        await session.execute(select(TicketStatusLog).where(TicketStatusLog.ticket_id == ticket.id).order_by(TicketStatusLog.created_at.desc()))
    ).scalars().all()
    source_email = await session.get(Email, ticket.source_email_id) if ticket.source_email_id else None
    thread = await session.get(EmailThread, ticket.thread_id) if ticket.thread_id else None
    detail = {
        "ticket": serialize_ticket(ticket),
        "items": [serialize_item(item) for item in items],
        "source_email": serialize_email(source_email) if source_email else None,
        "thread": model_to_dict(
            thread,
            (
                "id",
                "thread_key",
                "normalized_subject",
                "root_message_id",
                "latest_email_id",
                "ticket_id",
                "email_count",
                "merge_confidence",
                "merge_reason",
                "manual_locked",
                "created_at",
                "updated_at",
            ),
        )
        if thread
        else None,
        "parse_results": [serialize_parse_result(parse_result) for parse_result in parse_results],
        "sn_validation_results": [
            model_to_dict(
                row,
                (
                    "id",
                    "ticket_id",
                    "ticket_item_id",
                    "sn",
                    "matched_sn_asset_id",
                    "check_exists",
                    "check_valid",
                    "check_customer_match",
                    "check_material_match",
                    "need_ship_to_beijing",
                    "result_status",
                    "result_message",
                    "checked_by",
                    "checked_at",
                ),
            )
            for row in validations
        ],
        "manual_tasks": [
            model_to_dict(
                row,
                (
                    "id",
                    "ticket_id",
                    "email_id",
                    "task_type",
                    "priority",
                    "status",
                    "description",
                    "trigger_reason",
                    "assigned_user_id",
                    "claimed_by_user_id",
                    "claimed_at",
                    "resolved_by_user_id",
                    "resolved_at",
                    "resolution",
                    "created_at",
                    "updated_at",
                ),
            )
            for row in tasks
        ],
        "reply_records": [
            model_to_dict(
                row,
                (
                    "id",
                    "ticket_id",
                    "related_email_id",
                    "outgoing_email_id",
                    "template_id",
                    "reply_type",
                    "followup_round",
                    "missing_fields",
                    "to_addresses",
                    "cc_addresses",
                    "subject",
                    "draft_body",
                    "final_body",
                    "generate_source",
                    "review_status",
                    "reviewed_by_user_id",
                    "reviewed_at",
                    "send_status",
                    "smtp_message_id",
                    "sent_at",
                    "error_message",
                    "created_at",
                    "updated_at",
                ),
            )
            for row in replies
        ],
        "status_logs": [
            model_to_dict(
                row,
                (
                    "id",
                    "ticket_id",
                    "from_status_code",
                    "to_status_code",
                    "trigger_event",
                    "reason",
                    "operator_type",
                    "operator_user_id",
                    "metadata_json",
                    "created_at",
                ),
            )
            for row in status_logs
        ],
    }
    detail["email_timeline"] = await get_ticket_email_timeline(session, ticket.id)
    detail["attachments"] = await get_ticket_attachments(session, ticket.id)
    detail["field_evidence"] = await get_ticket_field_evidence(session, ticket.id)
    return detail


def _audit_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(to_plain(value), ensure_ascii=False, default=str)
    return str(to_plain(value))


def _coerce_ticket_value(field: str, value: Any) -> Any:
    if field == "request_date" and isinstance(value, str) and value:
        return date.fromisoformat(value)
    return value


async def patch_ticket_fields(
    session: AsyncSession,
    *,
    ticket_id: int,
    fields: dict[str, Any],
    user_id: int,
    reason: str | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    ticket = await get_ticket(session, ticket_id)
    if version is not None and ticket.version != version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="TICKET_VERSION_CONFLICT")
    changed: dict[str, Any] = {}
    for field, raw_value in fields.items():
        if field not in TICKET_WRITE_FIELDS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"TICKET_FIELD_NOT_ALLOWED:{field}")
        value = _coerce_ticket_value(field, raw_value)
        old_value = getattr(ticket, field)
        if to_plain(old_value) == to_plain(value):
            continue
        setattr(ticket, field, value)
        session.add(
            FieldAuditLog(
                ticket_id=ticket.id,
                field_name=field,
                old_value=_audit_value(old_value),
                new_value=_audit_value(value),
                source_type="manual",
                reason=reason,
                operator_user_id=user_id,
            )
        )
        changed[field] = {"old": to_plain(old_value), "new": to_plain(value)}
    if changed:
        ticket.version += 1
        await log_operation(
            session,
            user_id=user_id,
            operation_type="ticket_fields_updated",
            target_type="repair_ticket",
            target_id=ticket.id,
            description=reason,
            after_data=changed,
        )
    return await get_ticket_detail(session, ticket.id)


async def upsert_ticket_items(
    session: AsyncSession,
    *,
    ticket_id: int,
    items: list[Any],
    user_id: int,
    reason: str | None = None,
) -> dict[str, Any]:
    ticket = await get_ticket(session, ticket_id)
    existing_items = {
        item.id: item
        for item in (
            await session.execute(select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id))
        ).scalars().all()
    }
    max_line_no = max((item.line_no for item in existing_items.values()), default=0)
    changed: list[dict[str, Any]] = []
    for payload in items:
        data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else dict(payload)
        item_id = data.pop("id", None)
        item = existing_items.get(item_id) if item_id else None
        if item is None:
            max_line_no += 1
            item = RepairTicketItem(ticket_id=ticket.id, line_no=data.get("line_no") or max_line_no)
            session.add(item)
            await session.flush()
        item_changes: dict[str, Any] = {"id": item.id}
        for field, value in data.items():
            if field not in ITEM_WRITE_FIELDS:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"TICKET_ITEM_FIELD_NOT_ALLOWED:{field}")
            old_value = getattr(item, field)
            if to_plain(old_value) == to_plain(value):
                continue
            setattr(item, field, value)
            session.add(
                FieldAuditLog(
                    ticket_id=ticket.id,
                    ticket_item_id=item.id,
                    field_name=field,
                    old_value=_audit_value(old_value),
                    new_value=_audit_value(value),
                    source_type="manual",
                    reason=reason,
                    operator_user_id=user_id,
                )
            )
            item_changes[field] = {"old": to_plain(old_value), "new": to_plain(value)}
        if len(item_changes) > 1:
            changed.append(item_changes)
    if changed:
        ticket.version += 1
        await log_operation(
            session,
            user_id=user_id,
            operation_type="ticket_items_updated",
            target_type="repair_ticket",
            target_id=ticket.id,
            description=reason,
            after_data={"items": changed},
        )
    return await get_ticket_detail(session, ticket.id)


async def create_ticket_from_parse_result(session: AsyncSession, email: Email, parse_result: ParseResult) -> RepairTicket:
    fields = parse_result.extracted_fields or {}
    ticket = RepairTicket(
        ticket_no=f"RMA{utcnow():%Y%m%d%H%M%S%f}",
        current_status_code="new_email",
        source_email_id=email.id,
        thread_id=email.thread_id,
        customer_code=fields.get("customer_code"),
        customer_name=fields.get("customer_name"),
        contact_person=fields.get("contact_person"),
        contact_phone=fields.get("contact_phone"),
        contact_email=fields.get("contact_email") or email.from_address,
        problem_description=fields.get("problem_description"),
        missing_fields=parse_result.missing_fields,
        conflict_fields=parse_result.conflict_fields,
        confidence_score=parse_result.confidence_score,
        max_followup_count=settings.MAX_FOLLOW_UP,
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketStatusLog(
            ticket_id=ticket.id,
            from_status_code=None,
            to_status_code="new_email",
            trigger_event="ticket_created",
            reason="规则解析创建工单。",
            operator_type="system",
            metadata_json={"email_id": email.id, "parse_result_id": parse_result.id},
        )
    )
    session.add(EmailTicketLink(email_id=email.id, ticket_id=ticket.id, link_type="source", link_reason="规则解析创建"))
    if email.thread_id:
        thread = await session.get(EmailThread, email.thread_id)
        if thread:
            thread.ticket_id = ticket.id
    parse_result.ticket_id = ticket.id
    await _create_items_from_parse_result(session, ticket, parse_result, user_id=None)
    await log_operation(
        session,
        operation_type="ticket_created_from_parse",
        target_type="repair_ticket",
        target_id=ticket.id,
        description="规则解析创建工单。",
        after_data={"email_id": email.id, "parse_result_id": parse_result.id},
    )
    return ticket


async def _create_items_from_parse_result(
    session: AsyncSession,
    ticket: RepairTicket,
    parse_result: ParseResult,
    user_id: int | None,
) -> None:
    extracted = parse_result.extracted_items or {}
    if isinstance(extracted, dict):
        item_payloads = extracted.get("items", [])
    elif isinstance(extracted, list):
        item_payloads = extracted
    else:
        item_payloads = []
    existing_sns = set(
        (
            await session.execute(
                select(RepairTicketItem.sn).where(RepairTicketItem.ticket_id == ticket.id, RepairTicketItem.sn.is_not(None))
            )
        ).scalars().all()
    )
    max_line_no = int(
        (
            await session.execute(select(RepairTicketItem.line_no).where(RepairTicketItem.ticket_id == ticket.id).order_by(RepairTicketItem.line_no.desc()))
        ).scalars().first()
        or 0
    )
    for payload in item_payloads:
        sn = (payload.get("sn") or "").strip().upper() if isinstance(payload, dict) else ""
        if sn and sn in existing_sns:
            continue
        max_line_no += 1
        item = RepairTicketItem(
            ticket_id=ticket.id,
            line_no=payload.get("line_no") or max_line_no,
            material_code=payload.get("material_code"),
            material_name=payload.get("material_name"),
            sn=sn or None,
            quantity=payload.get("quantity") or 1,
            failure_description=payload.get("failure_description") or ticket.problem_description,
        )
        session.add(item)
        await session.flush()
        session.add(
            FieldAuditLog(
                ticket_id=ticket.id,
                ticket_item_id=item.id,
                field_name="item",
                old_value=None,
                new_value=_audit_value(payload),
                source_type="parse_result",
                reason="采纳解析结果创建明细。",
                operator_user_id=user_id,
                parse_result_id=parse_result.id,
            )
        )


async def apply_parse_result(
    session: AsyncSession,
    *,
    parse_result_id: int,
    user_id: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    parse_result = await session.get(ParseResult, parse_result_id)
    if parse_result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PARSE_RESULT_NOT_FOUND")
    email = await session.get(Email, parse_result.email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    ticket = await session.get(RepairTicket, parse_result.ticket_id) if parse_result.ticket_id else None
    if ticket is None:
        ticket = await create_ticket_from_parse_result(session, email, parse_result)

    fields = parse_result.extracted_fields or {}
    changed: dict[str, Any] = {}
    if not ticket.manual_locked:
        for field, value in fields.items():
            if field not in TICKET_WRITE_FIELDS:
                continue
            old_value = getattr(ticket, field)
            if to_plain(old_value) == to_plain(value):
                continue
            setattr(ticket, field, _coerce_ticket_value(field, value))
            session.add(
                FieldAuditLog(
                    ticket_id=ticket.id,
                    field_name=field,
                    old_value=_audit_value(old_value),
                    new_value=_audit_value(value),
                    source_type="parse_result",
                    reason=reason,
                    operator_user_id=user_id,
                    parse_result_id=parse_result.id,
                )
            )
            changed[field] = {"old": to_plain(old_value), "new": to_plain(value)}
    ticket.missing_fields = parse_result.missing_fields
    ticket.conflict_fields = parse_result.conflict_fields
    ticket.confidence_score = parse_result.confidence_score
    await _create_items_from_parse_result(session, ticket, parse_result, user_id)

    parse_result.accepted = True
    parse_result.accepted_by_user_id = user_id
    parse_result.accepted_at = utcnow()
    email.parse_status = "parsed"
    email.intent_type = parse_result.intent_type or email.intent_type
    ticket.version += 1
    await log_operation(
        session,
        user_id=user_id,
        operation_type="parse_result_applied",
        target_type="parse_result",
        target_id=parse_result.id,
        description=reason,
        after_data={"ticket_id": ticket.id, "changed_fields": changed},
    )

    if ticket.current_status_code == "new_email":
        if parse_result.confidence_score is not None and float(parse_result.confidence_score) < settings.CONFIDENCE_THRESHOLD:
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="manual_review",
                trigger_event="parse_low_confidence",
                user_id=user_id,
                operator_type="user" if user_id else "system",
                reason="规则解析置信度低，需要人工复核。",
                metadata={"parse_result_id": parse_result.id},
            )
        else:
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="parsed",
                trigger_event="parse_completed",
                user_id=user_id,
                operator_type="user" if user_id else "system",
                reason="解析结果已采纳。",
                metadata={"parse_result_id": parse_result.id},
            )
    if ticket.current_status_code == "parsed" and ticket.missing_fields:
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="need_customer_info",
            trigger_event="missing_fields_detected",
            user_id=user_id,
            operator_type="user" if user_id else "system",
            reason="解析后仍缺少关键字段。",
            metadata={"parse_result_id": parse_result.id, "missing_fields": ticket.missing_fields},
        )
    return await get_ticket_detail(session, ticket.id)


async def validate_ticket_sn(
    session: AsyncSession,
    *,
    ticket_id: int,
    user_id: int | None = None,
) -> dict[str, Any]:
    ticket = await get_ticket(session, ticket_id)
    items = (
        await session.execute(select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id).order_by(RepairTicketItem.line_no))
    ).scalars().all()
    if not items:
        ticket.missing_fields = {**(ticket.missing_fields or {}), "items": "缺少报修明细。"}
        if ticket.current_status_code == "parsed":
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="need_customer_info",
                trigger_event="missing_fields_detected",
                user_id=user_id,
                reason="缺少报修明细。",
            )
        return await get_ticket_detail(session, ticket.id)

    has_problem = False
    has_warning = False
    for item in items:
        result_status = "pending"
        message = "等待校验。"
        matched_asset = None
        board_card = None
        if not item.sn:
            result_status = "failed"
            message = "缺少 SN。"
        else:
            matched_asset = await session.scalar(select(SnAsset).where(SnAsset.sn == item.sn.strip().upper()))
            if matched_asset is None:
                result_status = "failed"
                message = "SN 不存在于资产库。"
            elif matched_asset.asset_status != "valid":
                result_status = "failed"
                message = f"SN 状态为 {matched_asset.asset_status}。"
            else:
                customer_match = not ticket.customer_code or ticket.customer_code == matched_asset.customer_code
                material_match = not item.material_code or item.material_code == matched_asset.material_code
                board_card = await session.scalar(select(BoardCard).where(BoardCard.material_code == matched_asset.material_code))
                if not customer_match:
                    result_status = "warning"
                    message = "SN 对应客户与工单客户不一致。"
                elif not material_match:
                    result_status = "warning"
                    message = "SN 对应物料与工单明细不一致。"
                else:
                    result_status = "pass"
                    message = "SN 校验通过。"
                item.sn_asset_id = matched_asset.id
                if not item.material_code:
                    item.material_code = matched_asset.material_code
                    item.material_name = matched_asset.material_name
        item.validation_status = result_status
        item.validation_message = message
        session.add(
            SnValidationResult(
                ticket_id=ticket.id,
                ticket_item_id=item.id,
                sn=item.sn or "",
                matched_sn_asset_id=matched_asset.id if matched_asset else None,
                check_exists=matched_asset is not None,
                check_valid=matched_asset.asset_status == "valid" if matched_asset else False,
                check_customer_match=(not ticket.customer_code or ticket.customer_code == matched_asset.customer_code) if matched_asset else False,
                check_material_match=(not item.material_code or item.material_code == matched_asset.material_code) if matched_asset else False,
                need_ship_to_beijing=board_card.need_ship_to_beijing if board_card else None,
                result_status=result_status,
                result_message=message,
                checked_by="manual" if user_id else "system",
            )
        )
        has_problem = has_problem or result_status == "failed"
        has_warning = has_warning or result_status == "warning"

    ticket.version += 1
    await log_operation(
        session,
        user_id=user_id,
        operation_type="ticket_sn_validated",
        target_type="repair_ticket",
        target_id=ticket.id,
        after_data={"has_problem": has_problem, "has_warning": has_warning},
    )
    if ticket.current_status_code == "parsed":
        if has_problem or has_warning:
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="manual_review",
                trigger_event="field_conflict",
                user_id=user_id,
                reason="SN 校验存在异常或警告。",
            )
        elif not ticket.missing_fields:
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="ready_for_export",
                trigger_event="validation_passed",
                user_id=user_id,
                reason="字段完整且 SN 校验通过。",
            )
    return await get_ticket_detail(session, ticket.id)


async def get_ticket_email_timeline(session: AsyncSession, ticket_id: int) -> list[dict[str, Any]]:
    await get_ticket(session, ticket_id)
    statement = (
        select(Email)
        .join(EmailTicketLink, EmailTicketLink.email_id == Email.id)
        .where(EmailTicketLink.ticket_id == ticket_id)
        .order_by(Email.received_at.asc(), Email.id.asc())
    )
    emails = (await session.execute(statement)).scalars().all()
    return [serialize_email(email) for email in emails]


async def get_ticket_attachments(session: AsyncSession, ticket_id: int) -> list[dict[str, Any]]:
    await get_ticket(session, ticket_id)
    statement = (
        select(EmailAttachment)
        .join(Email, Email.id == EmailAttachment.email_id)
        .join(EmailTicketLink, EmailTicketLink.email_id == Email.id)
        .where(EmailTicketLink.ticket_id == ticket_id)
        .order_by(EmailAttachment.created_at.desc())
    )
    attachments = (await session.execute(statement)).scalars().all()
    return [
        model_to_dict(
            attachment,
            (
                "id",
                "email_id",
                "oss_object_id",
                "file_name",
                "content_type",
                "file_size",
                "file_hash",
                "is_inline",
                "content_id",
                "parse_status",
                "extracted_text",
                "extracted_json",
                "parse_error",
                "created_at",
            ),
        )
        for attachment in attachments
    ]


async def get_ticket_field_evidence(session: AsyncSession, ticket_id: int) -> dict[str, Any]:
    await get_ticket(session, ticket_id)
    parse_results = (
        await session.execute(select(ParseResult).where(ParseResult.ticket_id == ticket_id).order_by(ParseResult.created_at.desc()))
    ).scalars().all()
    audits = (
        await session.execute(select(FieldAuditLog).where(FieldAuditLog.ticket_id == ticket_id).order_by(FieldAuditLog.created_at.desc()))
    ).scalars().all()
    return {
        "parse_evidence": [serialize_parse_result(parse_result) for parse_result in parse_results],
        "field_audits": [
            model_to_dict(
                audit,
                (
                    "id",
                    "ticket_id",
                    "ticket_item_id",
                    "field_name",
                    "old_value",
                    "new_value",
                    "source_type",
                    "reason",
                    "operator_user_id",
                    "parse_result_id",
                    "created_at",
                ),
            )
            for audit in audits
        ],
    }
