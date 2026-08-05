from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.repair_items import normalize_repair_item
from app.models import (
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    ExportSap,
    ExternalOperationRecord,
    FieldAuditLog,
    ManualReviewTask,
    ParseResult,
    RepairTicket,
    RepairTicketItem,
    ReplyRecord,
    SnValidationResult,
    TicketStatusLog,
    TicketRelayExport,
    TicketRma,
    WorkflowStatus,
)
from app.services.audit import log_operation
from app.services.business_rules import FOLLOWUP_REPLY_TYPES, required_missing_for_ticket
from app.services.common import model_to_dict, paginate_scalars, to_plain, utcnow
from app.services.master_data import xlsx_workbook_bytes
from app.services.workflow import create_manual_task_if_missing, transition_ticket

TICKET_FIELDS = (
    "id",
    "ticket_no",
    "current_status_code",
    "source_email_id",
    "thread_id",
    "customer_code",
    "customer_name",
    "customer_scope",
    "customer_scope_source",
    "charge_status",
    "charge_status_source",
    "service_policy_id",
    "policy_resolution_status",
    "policy_snapshot",
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
    "language_code",
    "rma_required",
    "relay_export_status",
    "rma_status",
    "sn_validation_status",
    "sn_validation_snapshot",
    "sn_validation_hash",
    "sn_validated_at",
    "safety_check_snapshot",
    "safety_check_hash",
    "safety_checked_at",
    "device_received_at",
    "device_received_source",
    "device_received_email_id",
    "device_received_note",
    "device_received_idempotency_key",
    "device_receipt_ack_status",
    "terminal_reason_code",
    "terminal_reason",
    "closed_at",
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
    "manual_locked",
}

ITEM_FIELDS = (
    "id",
    "ticket_id",
    "line_no",
    "material_code",
    "material_name",
    "board_code",
    "board_name",
    "matched_board_card_id",
    "return_location",
    "return_address",
    "return_contact",
    "return_phone",
    "return_postal_code",
    "return_route_source",
    "return_route_status",
    "return_route_message",
    "return_route_snapshot",
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
    "board_code",
    "board_name",
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
    "fetch_job_run_id",
    "raw_eml_oss_object_id",
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
    "processing_stage",
    "intent_type",
    "intent_subtype",
    "duplicate_of_email_id",
    "terminal_reason_code",
    "last_error_code",
    "retryable",
    "recovery_stage",
    "next_retry_at",
    "error_message",
    "created_at",
    "updated_at",
)


async def _next_ticket_no(session: AsyncSession) -> str:
    """Serialize daily ticket-number allocation without adding a database sequence."""
    await session.scalar(
        select(WorkflowStatus)
        .where(WorkflowStatus.status_code == "new_email")
        .with_for_update()
    )
    today_prefix = f"0{utcnow():%Y%m%d}"
    max_ticket_no = await session.scalar(
        select(func.max(RepairTicket.ticket_no)).where(
            RepairTicket.ticket_no.like(f"{today_prefix}%"),
            RepairTicket.ticket_no.op("REGEXP")(r"^0\d{10}$"),
        )
    )
    sequence = int(max_ticket_no[9:11]) + 1 if max_ticket_no else 1
    return f"{today_prefix}{sequence:02d}" if sequence < 100 else f"0{utcnow():%Y%m%d%H%M%S%f}"


def _attachment_file_size_kb(file_size: int | None) -> int | None:
    if file_size is None:
        return None
    return max(1, (int(file_size) + 1023) // 1024)


def _serialize_attachment_with_email(attachment: EmailAttachment, email: Email | None) -> dict[str, Any]:
    data = model_to_dict(
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
    data["file_size_kb"] = _attachment_file_size_kb(attachment.file_size)
    data["sent_at"] = to_plain((email.sent_at or email.received_at) if email else None)
    return data


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
            "intent_subtype",
            "extracted_fields",
            "extracted_items",
            "missing_fields",
            "conflict_fields",
            "confidence_score",
            "field_confidences",
            "evidence",
            "apply_status",
            "applied_by_user_id",
            "applied_at",
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
    ticket_no: str | None = None,
    customer: str | None = None,
    contact: str | None = None,
    sn: str | None = None,
    assigned_user_id: int | None = None,
    request_date_start: date | None = None,
    request_date_end: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    statement = _ticket_filter_statement(
        status_code=status_code,
        keyword=keyword,
        ticket_no=ticket_no,
        customer=customer,
        contact=contact,
        sn=sn,
        assigned_user_id=assigned_user_id,
        request_date_start=request_date_start,
        request_date_end=request_date_end,
    )
    statement = statement.order_by(RepairTicket.updated_at.desc(), RepairTicket.id.desc())
    tickets, total = await paginate_scalars(session, statement, page, page_size)
    return [serialize_ticket(ticket) for ticket in tickets], total


def _ticket_filter_statement(
    *,
    status_code: str | None = None,
    keyword: str | None = None,
    ticket_no: str | None = None,
    customer: str | None = None,
    contact: str | None = None,
    sn: str | None = None,
    assigned_user_id: int | None = None,
    request_date_start: date | None = None,
    request_date_end: date | None = None,
):
    statement = select(RepairTicket)
    if status_code:
        statement = statement.where(RepairTicket.current_status_code == status_code)
    if ticket_no:
        statement = statement.where(RepairTicket.ticket_no.like(f"%{ticket_no}%"))
    if customer:
        like = f"%{customer}%"
        statement = statement.where(or_(RepairTicket.customer_code.like(like), RepairTicket.customer_name.like(like)))
    if contact:
        like = f"%{contact}%"
        statement = statement.where(
            or_(RepairTicket.contact_person.like(like), RepairTicket.contact_phone.like(like), RepairTicket.contact_email.like(like))
        )
    if assigned_user_id:
        statement = statement.where(RepairTicket.assigned_user_id == assigned_user_id)
    if request_date_start:
        statement = statement.where(RepairTicket.request_date >= request_date_start)
    if request_date_end:
        statement = statement.where(RepairTicket.request_date <= request_date_end)
    if sn:
        statement = statement.where(
            RepairTicket.id.in_(
                select(RepairTicketItem.ticket_id).where(RepairTicketItem.sn.like(f"%{sn.strip().upper()}%"))
            )
        )
    if keyword:
        like = f"%{keyword}%"
        statement = statement.where(
            or_(
                RepairTicket.ticket_no.like(like),
                RepairTicket.customer_code.like(like),
                RepairTicket.customer_name.like(like),
                RepairTicket.contact_person.like(like),
                RepairTicket.contact_email.like(like),
                RepairTicket.problem_description.like(like),
            )
        )
    return statement


async def export_tickets(
    session: AsyncSession,
    *,
    status_code: str | None = None,
    keyword: str | None = None,
    ticket_no: str | None = None,
    customer: str | None = None,
    contact: str | None = None,
    sn: str | None = None,
    assigned_user_id: int | None = None,
    request_date_start: date | None = None,
    request_date_end: date | None = None,
) -> list[dict[str, Any]]:
    statement = _ticket_filter_statement(
        status_code=status_code,
        keyword=keyword,
        ticket_no=ticket_no,
        customer=customer,
        contact=contact,
        sn=sn,
        assigned_user_id=assigned_user_id,
        request_date_start=request_date_start,
        request_date_end=request_date_end,
    ).order_by(RepairTicket.updated_at.desc(), RepairTicket.id.desc())
    rows = (await session.execute(statement)).scalars().all()
    export_rows: list[dict[str, Any]] = []
    for ticket in rows:
        data = serialize_ticket(ticket)
        data["missing_fields_json"] = json.dumps(to_plain(ticket.missing_fields), ensure_ascii=False) if ticket.missing_fields else ""
        data["conflict_fields_json"] = json.dumps(to_plain(ticket.conflict_fields), ensure_ascii=False) if ticket.conflict_fields else ""
        data["attachment_summary"] = await _attachment_export_summary(session, ticket.id)
        data["sn_validation_summary"] = await _sn_validation_export_summary(session, ticket.id)
        data["reply_status_summary"] = await _reply_export_summary(session, ticket.id)
        export_rows.append(data)
    return export_rows


def _ticket_export_rows(ticket: RepairTicket, items: list[RepairTicketItem]) -> list[dict[str, Any]]:
    rows = [
        {"section": "工单", "field": "工单号", "value": ticket.ticket_no},
        {"section": "工单", "field": "状态", "value": ticket.current_status_code},
        {"section": "客户", "field": "客户编码", "value": ticket.customer_code},
        {"section": "客户", "field": "客户名称", "value": ticket.customer_name},
        {"section": "联系人", "field": "联系人", "value": ticket.contact_person},
        {"section": "联系人", "field": "电话", "value": ticket.contact_phone},
        {"section": "联系人", "field": "邮箱", "value": ticket.contact_email},
        {"section": "处理", "field": "处理人ID", "value": ticket.assigned_user_id},
        {"section": "处理", "field": "请求日期", "value": ticket.request_date},
        {"section": "处理", "field": "置信度", "value": ticket.confidence_score},
        {"section": "处理", "field": "追问次数", "value": ticket.followup_count},
        {"section": "内容", "field": "故障描述", "value": ticket.problem_description},
        {"section": "内容", "field": "缺失字段", "value": json.dumps(to_plain(ticket.missing_fields), ensure_ascii=False) if ticket.missing_fields else ""},
        {"section": "内容", "field": "冲突字段", "value": json.dumps(to_plain(ticket.conflict_fields), ensure_ascii=False) if ticket.conflict_fields else ""},
    ]
    for item in items:
        rows.extend(
            [
                {"section": f"明细{item.line_no}", "field": "SN", "value": item.sn},
                {"section": f"明细{item.line_no}", "field": "物料编码", "value": item.material_code},
                {"section": f"明细{item.line_no}", "field": "物料名称", "value": item.material_name},
                {"section": f"明细{item.line_no}", "field": "故障现象", "value": item.failure_description},
                {"section": f"明细{item.line_no}", "field": "数量", "value": item.quantity},
            ]
        )
    return rows


async def _attachment_export_summary(session: AsyncSession, ticket_id: int) -> str:
    attachments = await get_ticket_attachments(session, ticket_id)
    parts: list[str] = []
    for attachment in attachments:
        extracted = attachment.get("extracted_json") or {}
        summary = extracted.get("summary") if isinstance(extracted, dict) else None
        parts.append(
            f"{attachment.get('file_name') or '-'}[{attachment.get('parse_status') or '-'}]"
            + (f": {summary}" if summary else "")
        )
    return "\n".join(parts)


async def _sn_validation_export_summary(session: AsyncSession, ticket_id: int) -> str:
    rows = (
        await session.execute(select(SnValidationResult).where(SnValidationResult.ticket_id == ticket_id).order_by(SnValidationResult.checked_at.desc()))
    ).scalars().all()
    return "\n".join(f"{row.sn}: {row.result_status} {row.result_message or ''}".strip() for row in rows)


async def _reply_export_summary(session: AsyncSession, ticket_id: int) -> str:
    rows = (
        await session.execute(select(ReplyRecord).where(ReplyRecord.ticket_id == ticket_id).order_by(ReplyRecord.created_at.desc()))
    ).scalars().all()
    return "\n".join(f"{row.reply_type}: review={row.review_status}, send={row.send_status}" for row in rows)


async def export_tickets_selected(session: AsyncSession, *, ids: list[int]) -> bytes:
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EXPORT_SELECTION_REQUIRED")
    tickets = (await session.execute(select(RepairTicket).where(RepairTicket.id.in_(ids)).order_by(RepairTicket.id))).scalars().all()
    if not tickets:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EXPORT_SELECTION_EMPTY")
    sheets: list[tuple[str, list[dict[str, Any]], list[str]]] = []
    for ticket in tickets:
        items = (
            await session.execute(select(RepairTicketItem).where(RepairTicketItem.ticket_id == ticket.id).order_by(RepairTicketItem.line_no))
        ).scalars().all()
        rows = _ticket_export_rows(ticket, list(items))
        rows.extend(
            [
                {"section": "附件", "field": "附件解析摘要", "value": await _attachment_export_summary(session, ticket.id)},
                {"section": "SN校验", "field": "校验摘要", "value": await _sn_validation_export_summary(session, ticket.id)},
                {"section": "回复", "field": "回复状态", "value": await _reply_export_summary(session, ticket.id)},
            ]
        )
        sheets.append((ticket.ticket_no, rows, ["section", "field", "value"]))
    return xlsx_workbook_bytes(sheets)


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
    relay_batches = (
        await session.execute(
            select(TicketRelayExport)
            .where(TicketRelayExport.ticket_id == ticket.id)
            .order_by(TicketRelayExport.created_at.desc())
        )
    ).scalars().all()
    sap_exports = (
        await session.execute(
            select(ExportSap)
            .where(ExportSap.ticket_id == ticket.id)
            .order_by(ExportSap.ticket_item_id, ExportSap.created_at.desc())
        )
    ).scalars().all()
    rma_rows = (
        await session.execute(
            select(TicketRma)
            .where(TicketRma.ticket_id == ticket.id)
            .order_by(TicketRma.created_at)
        )
    ).scalars().all()
    external_operations = (
        await session.execute(
            select(ExternalOperationRecord)
            .where(ExternalOperationRecord.ticket_id == ticket.id)
            .order_by(ExternalOperationRecord.created_at.desc())
        )
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
                "predecessor_thread_id",
                "predecessor_ticket_id",
                "thread_version",
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
                    "ticket_version",
                    "input_hash",
                    "source_system",
                    "evidence_json",
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
                    "recovery_stage",
                    "recovery_action",
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
                    "draft_html_body",
                    "final_html_body",
                    "thread_history_hash",
                    "render_hash",
                    "generate_source",
                    "reply_template_version",
                    "rma_template_version",
                    "rma_pdf_oss_object_id",
                    "rma_pdf_data_snapshot",
                    "review_status",
                    "reviewed_by_user_id",
                    "reviewed_at",
                    "send_status",
                    "archive_status",
                    "send_attempt_count",
                    "archive_attempt_count",
                    "smtp_message_id",
                    "smtp_response",
                    "thread_version",
                    "in_reply_to",
                    "references_header",
                    "sent_at",
                    "archive_verified_at",
                    "next_retry_at",
                    "last_error_code",
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
        "sap_export_summary": {
            "batch_status": relay_batches[0].status if relay_batches else "not_started",
            "line_count": len(sap_exports),
            "submitted_count": sum(
                1 for row in sap_exports if row.status in {"accepted", "waiting_rma", "rma_received"}
            ),
            "accepted_count": sum(
                1 for row in sap_exports if row.remote_call_id
            ),
            "rma_received_count": sum(
                1 for row in sap_exports if row.status == "rma_received" and row.rma_no
            ),
            "failed_count": sum(
                1 for row in sap_exports if row.status in {"failed", "timed_out", "manual_review"}
            ),
        },
        "sap_exports": [
            model_to_dict(
                row,
                (
                    "id",
                    "ticket_item_id",
                    "relay_export_id",
                    "submission_key",
                    "status",
                    "attempt_count",
                    "remote_call_id",
                    "rma_no",
                    "last_error_code",
                    "last_error_message",
                    "submitted_at",
                    "accepted_at",
                    "last_polled_at",
                    "rma_received_at",
                    "sn",
                    "customer_code",
                    "material_code",
                    "customer_name",
                    "material_name",
                    "currency",
                    "shipping_fee",
                    "repair_fee",
                    "tax_rate",
                ),
            )
            for row in sap_exports
        ],
        "rma_records": [
            model_to_dict(
                row,
                (
                    "id",
                    "rma_no",
                    "status",
                    "policy_snapshot",
                    "pdf_oss_object_id",
                    "pdf_sha256",
                    "pdf_validation_status",
                    "pdf_archive_status",
                    "reply_record_id",
                    "received_at",
                    "sent_at",
                    "pdf_archived_at",
                    "issued_at",
                    "created_at",
                    "updated_at",
                ),
            )
            for row in rma_rows
        ],
        "external_operations": [
            model_to_dict(
                row,
                (
                    "id",
                    "operation_type",
                    "operation_key",
                    "status",
                    "ticket_id",
                    "email_id",
                    "reply_record_id",
                    "export_sap_id",
                    "attempt_count",
                    "remote_reference",
                    "error_code",
                    "error_message",
                    "retryable",
                    "recovery_stage",
                    "next_retry_at",
                    "started_at",
                    "completed_at",
                    "details_json",
                    "created_at",
                    "updated_at",
                ),
            )
            for row in external_operations
        ],
    }
    rma_reply = next(
        (
            row
            for row in replies
            if row.reply_type == "rma_authorization" and row.send_status == "sent"
        ),
        None,
    )
    rma_record = rma_rows[0] if len(rma_rows) == 1 else None
    rma_outgoing = (
        await session.get(Email, rma_reply.outgoing_email_id)
        if rma_reply and rma_reply.outgoing_email_id
        else None
    )
    rma_attachment = (
        await session.scalar(
            select(EmailAttachment).where(
                EmailAttachment.email_id == rma_reply.outgoing_email_id,
                EmailAttachment.oss_object_id == rma_reply.rma_pdf_oss_object_id,
            )
        )
        if rma_reply
        and rma_reply.outgoing_email_id
        and rma_reply.rma_pdf_oss_object_id
        else None
    )
    detail["rma_issue_summary"] = {
        "rma_received": bool(
            rma_record and rma_record.rma_no and rma_record.received_at
        ),
        "pdf_validated": bool(
            rma_record
            and rma_record.pdf_validation_status == "passed"
            and rma_record.pdf_oss_object_id
            and rma_record.pdf_sha256
        ),
        "smtp_sent": bool(
            rma_reply and rma_reply.send_status == "sent" and rma_reply.smtp_message_id
        ),
        "message_id_saved": bool(
            rma_reply
            and rma_reply.smtp_message_id
            and rma_outgoing
            and rma_outgoing.message_id == rma_reply.smtp_message_id
        ),
        "pdf_archived": bool(
            rma_record
            and rma_record.pdf_archive_status == "archived"
            and rma_record.pdf_archived_at
        ),
        "outbound_archived": bool(
            rma_reply
            and rma_reply.archive_status == "archived"
            and rma_reply.archive_verified_at
            and rma_outgoing
            and rma_outgoing.raw_eml_oss_object_id
            and rma_attachment
            and rma_record
            and rma_attachment.file_hash == rma_record.pdf_sha256
        ),
        "closed": ticket.current_status_code == "closed",
        "terminal_reason_code": ticket.terminal_reason_code,
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
    if ticket.rma_status == "sent":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="RMA_SENT_TICKET_DATA_IMMUTABLE")
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
        await _invalidate_export_snapshot(
            session,
            ticket=ticket,
            user_id=user_id,
            reason="ticket fields changed",
            invalidate_sn=bool({"customer_code", "customer_name"} & set(changed)),
        )
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
    if ticket.rma_status == "sent":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="RMA_SENT_TICKET_DATA_IMMUTABLE")
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
        sn_fields = {"sn", "material_code", "material_name"}
        await _invalidate_export_snapshot(
            session,
            ticket=ticket,
            user_id=user_id,
            reason="ticket items changed",
            invalidate_sn=any(sn_fields & set(change) for change in changed),
        )
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


async def _invalidate_export_snapshot(
    session: AsyncSession,
    *,
    ticket: RepairTicket,
    user_id: int | None,
    reason: str,
    invalidate_sn: bool = True,
) -> None:
    if invalidate_sn:
        ticket.sn_validation_status = "stale"
        ticket.sn_validation_snapshot = None
        ticket.sn_validation_hash = None
        ticket.sn_validated_at = None
    ticket.safety_check_snapshot = None
    ticket.safety_check_hash = None
    ticket.safety_checked_at = None
    ticket.relay_export_status = "not_required"
    ticket.rma_status = "not_required" if not ticket.rma_required else "pending"
    if ticket.current_status_code == "ready_for_export":
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="manual_review",
            trigger_event="validated_data_changed",
            user_id=user_id,
            operator_type="user" if user_id else "system",
            reason=reason,
            metadata={"invalidate_sn": invalidate_sn},
            manual_task_type="validated_data_changed",
            manual_task_priority="high",
        )


async def create_ticket_from_parse_result(
    session: AsyncSession,
    email: Email,
    parse_result: ParseResult,
    *,
    selected_fields: set[str] | None = None,
    selected_item_indices: set[int] | None = None,
) -> RepairTicket:
    from app.services.routing import choose_system_owner

    fields = parse_result.extracted_fields or {}
    if parse_result.intent_type not in {"new_repair", "customer_supplement"}:
        fields = {}
    if selected_fields is not None:
        fields = {key: value for key, value in fields.items() if key in selected_fields}
    owner_id, language_code, routing_reason = await choose_system_owner(session, email)
    ticket_no = await _next_ticket_no(session)
    ticket = RepairTicket(
        ticket_no=ticket_no,
        current_status_code="new_email",
        source_email_id=email.id,
        thread_id=email.thread_id,
        customer_code=fields.get("customer_code"),
        customer_name=fields.get("customer_name"),
        contact_person=fields.get("contact_person"),
        contact_phone=fields.get("contact_phone"),
        contact_email=fields.get("contact_email") or email.from_address,
        request_date=_coerce_ticket_value("request_date", fields.get("request_date")) if fields.get("request_date") else None,
        mailing_address=fields.get("mailing_address"),
        problem_description=fields.get("problem_description"),
        missing_fields=parse_result.missing_fields if selected_fields is None else {},
        conflict_fields=parse_result.conflict_fields if selected_fields is None else {},
        confidence_score=parse_result.confidence_score,
        language_code=language_code,
        assigned_user_id=owner_id,
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
            metadata_json={"email_id": email.id, "parse_result_id": parse_result.id, "routing": routing_reason},
        )
    )
    session.add(EmailTicketLink(email_id=email.id, ticket_id=ticket.id, link_type="source", link_reason="规则解析创建"))
    if email.thread_id:
        thread = await session.get(EmailThread, email.thread_id)
        if thread:
            thread.ticket_id = ticket.id
    parse_result.ticket_id = ticket.id
    parse_result.apply_status = "auto_applied"
    parse_result.applied_by_user_id = None
    parse_result.applied_at = utcnow()
    parse_result.accepted = True
    parse_result.accepted_by_user_id = None
    parse_result.accepted_at = parse_result.applied_at
    await _create_items_from_parse_result(
        session,
        ticket,
        parse_result,
        user_id=None,
        selected_item_indices=selected_item_indices,
    )
    await log_operation(
        session,
        operation_type="ticket_created_from_parse",
        target_type="repair_ticket",
        target_id=ticket.id,
        description="规则解析创建工单。",
        after_data={"email_id": email.id, "parse_result_id": parse_result.id},
    )
    return ticket


async def _existing_ticket_for_email(session: AsyncSession, email: Email, parse_result: ParseResult | None = None) -> RepairTicket | None:
    if parse_result and parse_result.ticket_id:
        ticket = await session.get(RepairTicket, parse_result.ticket_id)
        if ticket:
            return ticket
    if email.thread_id:
        thread = await session.get(EmailThread, email.thread_id)
        if thread and thread.ticket_id:
            ticket = await session.get(RepairTicket, thread.ticket_id)
            if ticket:
                return ticket
    link = await session.scalar(
        select(EmailTicketLink).where(EmailTicketLink.email_id == email.id).order_by(EmailTicketLink.created_at.desc(), EmailTicketLink.id.desc())
    )
    if link:
        return await session.get(RepairTicket, link.ticket_id)
    return None


async def _link_email_to_ticket(
    session: AsyncSession,
    *,
    email: Email,
    ticket: RepairTicket,
    link_type: str,
    link_reason: str,
) -> None:
    existing = await session.scalar(select(EmailTicketLink).where(EmailTicketLink.email_id == email.id, EmailTicketLink.ticket_id == ticket.id))
    if existing is None:
        session.add(EmailTicketLink(email_id=email.id, ticket_id=ticket.id, link_type=link_type, link_reason=link_reason))
    if email.thread_id:
        thread = await session.get(EmailThread, email.thread_id)
        if thread:
            thread.ticket_id = ticket.id


async def ensure_manual_review_ticket_from_parse_result(
    session: AsyncSession,
    *,
    email: Email,
    parse_result: ParseResult,
    reason: str,
    task_type: str = "manual_review_required",
) -> RepairTicket:
    ticket = await _existing_ticket_for_email(session, email, parse_result)
    fields = parse_result.extracted_fields or {}
    if parse_result.intent_type not in {"new_repair", "customer_supplement"}:
        fields = {}
    if ticket is None:
        from app.services.routing import choose_system_owner

        owner_id, language_code, routing_reason = await choose_system_owner(session, email)
        ticket_no = await _next_ticket_no(session)
        ticket = RepairTicket(
            ticket_no=ticket_no,
            current_status_code="manual_review",
            source_email_id=email.id,
            thread_id=email.thread_id,
            customer_code=fields.get("customer_code"),
            customer_name=fields.get("customer_name"),
            contact_person=fields.get("contact_person"),
            contact_phone=fields.get("contact_phone"),
            contact_email=fields.get("contact_email") or email.from_address,
            request_date=_coerce_ticket_value("request_date", fields.get("request_date")) if fields.get("request_date") else None,
            mailing_address=fields.get("mailing_address"),
            problem_description=fields.get("problem_description"),
            missing_fields=parse_result.missing_fields,
            conflict_fields=parse_result.conflict_fields,
            confidence_score=parse_result.confidence_score,
            language_code=language_code,
            assigned_user_id=owner_id,
            max_followup_count=settings.MAX_FOLLOW_UP,
        )
        session.add(ticket)
        await session.flush()
        session.add(
            TicketStatusLog(
                ticket_id=ticket.id,
                from_status_code=None,
                to_status_code="manual_review",
                trigger_event="manual_review_required",
                reason=reason,
                operator_type="system",
                metadata_json={"email_id": email.id, "parse_result_id": parse_result.id, "routing": routing_reason},
            )
        )
    else:
        ticket.missing_fields = parse_result.missing_fields
        ticket.conflict_fields = parse_result.conflict_fields
        ticket.confidence_score = parse_result.confidence_score
        if ticket.current_status_code not in {"manual_review", "closed"}:
            await transition_ticket(
                session,
                ticket=ticket,
                to_status_code="manual_review",
                trigger_event="manual_review_required",
                operator_type="system",
                reason=reason,
                metadata={"email_id": email.id, "parse_result_id": parse_result.id},
                manual_task_type=task_type,
            )
    parse_result.ticket_id = ticket.id
    parse_result.apply_status = "needs_manual_review"
    parse_result.accepted = False
    parse_result.accepted_by_user_id = None
    parse_result.accepted_at = None
    parse_result.error_message = reason
    await _link_email_to_ticket(session, email=email, ticket=ticket, link_type="related", link_reason=reason)
    await create_manual_task_if_missing(
        session,
        ticket=ticket,
        task_type=task_type,
        trigger_reason=reason,
        priority="high" if parse_result.conflict_fields else "normal",
        email_id=email.id,
        assigned_user_id=ticket.assigned_user_id,
    )
    await log_operation(
        session,
        operation_type="manual_review_ticket_created",
        target_type="repair_ticket",
        target_id=ticket.id,
        description=reason,
        after_data={"email_id": email.id, "parse_result_id": parse_result.id, "task_type": task_type},
    )
    return ticket


async def _create_items_from_parse_result(
    session: AsyncSession,
    ticket: RepairTicket,
    parse_result: ParseResult,
    user_id: int | None,
    selected_item_indices: set[int] | None = None,
) -> None:
    extracted = parse_result.extracted_items or {}
    if isinstance(extracted, dict):
        item_payloads = extracted.get("items", [])
    elif isinstance(extracted, list):
        item_payloads = extracted
    else:
        item_payloads = []
    existing_items = list(
        (
            await session.execute(
                select(RepairTicketItem)
                .where(RepairTicketItem.ticket_id == ticket.id)
                .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
            )
        ).scalars().all()
    )

    if parse_result.intent_type == "customer_supplement" and item_payloads:
        source_email = await session.get(Email, parse_result.email_id)
        supplement_text = (
            (source_email.latest_reply_segment if source_email is not None else None)
            or (source_email.clean_body if source_email is not None else None)
            or (source_email.text_body if source_email is not None else None)
            or ""
        )
        normalized_text = " ".join(str(supplement_text).lower().split())
        correction_markers = (
            "录入有误",
            "sn有误",
            "sn 有误",
            "更正为",
            "修正为",
            "改为",
            "replace the sn",
            "replace sn",
            "corrected sn",
            "sn was wrong",
        )
        if any(marker in normalized_text for marker in correction_markers):
            replacement_sns = {
                normalized["sn"]
                for index, payload in enumerate(item_payloads, start=1)
                if isinstance(payload, dict)
                and (normalized := normalize_repair_item(payload, default_line_no=index)).get("sn")
            }
            retained_items: list[RepairTicketItem] = []
            for existing_item in existing_items:
                existing_sn = str(existing_item.sn or "").strip().upper()
                if existing_item.manual_locked or not existing_sn or existing_sn in replacement_sns:
                    retained_items.append(existing_item)
                    continue
                session.add(
                    FieldAuditLog(
                        ticket_id=ticket.id,
                        ticket_item_id=existing_item.id,
                        field_name="item",
                        old_value=_audit_value(serialize_item(existing_item)),
                        new_value=None,
                        source_type="parse_result",
                        reason="Customer explicitly corrected the previously supplied SN set.",
                        operator_user_id=user_id,
                        parse_result_id=parse_result.id,
                    )
                )
                await session.delete(existing_item)
            existing_items = retained_items

    existing_sns = {str(item.sn).strip().upper() for item in existing_items if item.sn and str(item.sn).strip()}
    existing_by_line = {int(item.line_no): item for item in existing_items}
    max_line_no = max((int(item.line_no) for item in existing_items), default=0)
    reconciled_item_ids: set[int] = set()

    def is_parser_placeholder(item: RepairTicketItem) -> bool:
        return (
            not item.manual_locked
            and not (item.sn and str(item.sn).strip())
            and not (item.board_code and str(item.board_code).strip())
            and not (item.board_name and str(item.board_name).strip())
        )

    for item_index, payload in enumerate(item_payloads):
        if selected_item_indices is not None and item_index not in selected_item_indices:
            continue
        if isinstance(payload, dict):
            payload = normalize_repair_item(payload, default_line_no=item_index + 1)
        sn = (payload.get("sn") or "").strip().upper() if isinstance(payload, dict) else ""
        if sn and sn in existing_sns:
            continue
        requested_line_no = int(payload.get("line_no") or 0) if isinstance(payload, dict) else 0
        placeholder = existing_by_line.get(requested_line_no) if requested_line_no > 0 else None
        if placeholder is None or not is_parser_placeholder(placeholder):
            placeholder = next(
                (item for item in existing_items if item.id not in reconciled_item_ids and is_parser_placeholder(item)),
                None,
            )
        if placeholder is not None:
            old_value = _audit_value(serialize_item(placeholder))
            placeholder.sn = sn or None
            placeholder.board_code = (
                payload.get("board_code")
                or payload.get("board_model")
                or placeholder.board_code
            )
            placeholder.board_name = payload.get("board_name") or placeholder.board_name
            placeholder.quantity = payload.get("quantity") or placeholder.quantity or 1
            placeholder.failure_description = payload.get("failure_description") or placeholder.failure_description or ticket.problem_description
            placeholder.failure_information = payload.get("failure_information") or placeholder.failure_information
            placeholder.data_info = payload.get("data_info") or placeholder.data_info
            placeholder.remarks = payload.get("remarks") or placeholder.remarks
            placeholder.accessories = payload.get("accessories") or placeholder.accessories
            reconciled_item_ids.add(int(placeholder.id))
            if sn:
                existing_sns.add(sn)
            session.add(
                FieldAuditLog(
                    ticket_id=ticket.id,
                    ticket_item_id=placeholder.id,
                    field_name="item",
                    old_value=old_value,
                    new_value=_audit_value(payload),
                    source_type="parse_result",
                    reason="Reconciled parser placeholder with structured attachment item.",
                    operator_user_id=user_id,
                    parse_result_id=parse_result.id,
                )
            )
            continue
        max_line_no += 1
        line_no = requested_line_no if requested_line_no > 0 and requested_line_no not in existing_by_line else max_line_no
        item = RepairTicketItem(
            ticket_id=ticket.id,
            line_no=line_no,
            board_code=payload.get("board_code") or payload.get("board_model"),
            board_name=payload.get("board_name"),
            sn=sn or None,
            quantity=payload.get("quantity") or 1,
            failure_description=payload.get("failure_description") or ticket.problem_description,
        )
        session.add(item)
        await session.flush()
        existing_by_line[int(item.line_no)] = item
        if sn:
            existing_sns.add(sn)
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

    if item_payloads and reconciled_item_ids:
        for item in existing_items:
            if item.id not in reconciled_item_ids and is_parser_placeholder(item):
                await session.delete(item)


async def apply_parse_result(
    session: AsyncSession,
    *,
    parse_result_id: int,
    user_id: int | None = None,
    reason: str | None = None,
    action: str = "apply",
    apply_status: str | None = None,
    selected_fields: list[str] | None = None,
    selected_item_indices: list[int] | None = None,
) -> dict[str, Any]:
    parse_result = await session.get(ParseResult, parse_result_id)
    if parse_result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PARSE_RESULT_NOT_FOUND")
    now = utcnow()
    if action == "reject":
        if parse_result.ticket_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PARSE_RESULT_TICKET_NOT_FOUND")
        ticket = await get_ticket(session, parse_result.ticket_id)
        parse_result.apply_status = "rejected"
        parse_result.applied_by_user_id = user_id
        parse_result.applied_at = now
        parse_result.accepted = False
        parse_result.accepted_by_user_id = None
        parse_result.accepted_at = None
        await log_operation(
            session,
            user_id=user_id,
            operation_type="parse_result_rejected",
            target_type="parse_result",
            target_id=parse_result.id,
            description=reason,
            after_data={"ticket_id": ticket.id, "apply_status": "rejected"},
        )
        return await get_ticket_detail(session, ticket.id)
    if action not in {"apply", "partial_apply"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PARSE_RESULT_ACTION_INVALID")
    field_selection = set(selected_fields or []) if action == "partial_apply" else None
    item_selection = set(selected_item_indices or []) if action == "partial_apply" else None
    if action == "partial_apply" and not field_selection and not item_selection:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PARSE_RESULT_PARTIAL_SELECTION_REQUIRED")
    if item_selection is not None and any(index < 0 for index in item_selection):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PARSE_RESULT_ITEM_INDEX_INVALID")
    email = await session.get(Email, parse_result.email_id)
    if email is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EMAIL_NOT_FOUND")
    ticket = await _existing_ticket_for_email(session, email, parse_result)
    if ticket is None:
        ticket = await create_ticket_from_parse_result(
            session,
            email,
            parse_result,
            selected_fields=field_selection,
            selected_item_indices=item_selection,
        )
    else:
        parse_result.ticket_id = ticket.id
        await _link_email_to_ticket(
            session,
            email=email,
            ticket=ticket,
            link_type="related" if ticket.source_email_id != email.id else "source",
            link_reason=reason or "解析结果关联到同回复链工单",
        )

    fields = parse_result.extracted_fields or {}
    if parse_result.intent_type not in {"new_repair", "customer_supplement"}:
        fields = {}
    if field_selection is not None:
        fields = {key: value for key, value in fields.items() if key in field_selection}
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
    if action == "partial_apply":
        ticket.missing_fields = {
            key: value for key, value in (ticket.missing_fields or {}).items() if key not in field_selection
        }
        ticket.conflict_fields = {
            key: value for key, value in (ticket.conflict_fields or {}).items() if key not in field_selection
        }
    else:
        ticket.missing_fields = parse_result.missing_fields
        ticket.conflict_fields = parse_result.conflict_fields
    ticket.confidence_score = parse_result.confidence_score
    await _create_items_from_parse_result(
        session,
        ticket,
        parse_result,
        user_id,
        selected_item_indices=item_selection,
    )
    if parse_result.intent_type in {"new_repair", "customer_supplement"}:
        await session.flush()
        current_items = (
            await session.execute(
                select(RepairTicketItem)
                .where(RepairTicketItem.ticket_id == ticket.id)
                .order_by(RepairTicketItem.line_no, RepairTicketItem.id)
            )
        ).scalars().all()
        ticket.missing_fields = required_missing_for_ticket(ticket, current_items)
        if parse_result.intent_type == "customer_supplement":
            sent_followups = await session.scalar(
                select(func.count(ReplyRecord.id)).where(
                    ReplyRecord.ticket_id == ticket.id,
                    ReplyRecord.reply_type.in_(FOLLOWUP_REPLY_TYPES),
                    ReplyRecord.send_status == "sent",
                )
            )
            ticket.followup_count = min(ticket.max_followup_count, int(sent_followups or 0))

    result_status = apply_status or ("partially_applied" if action == "partial_apply" or ticket.manual_locked else ("manually_applied" if user_id else "auto_applied"))
    parse_result.apply_status = result_status
    parse_result.applied_by_user_id = user_id
    parse_result.applied_at = now
    parse_result.accepted = True
    parse_result.accepted_by_user_id = user_id
    parse_result.accepted_at = now
    email.parse_status = "parsed"
    email.intent_type = parse_result.intent_type or email.intent_type
    email.intent_subtype = parse_result.intent_subtype
    email.processing_stage = "completed"
    email.terminal_reason_code = "EMAIL_PROCESSING_COMPLETED"
    email.last_error_code = None
    email.retryable = False
    email.next_retry_at = None
    ticket.version += 1
    await log_operation(
        session,
        user_id=user_id,
        operation_type="parse_result_applied",
        target_type="parse_result",
        target_id=parse_result.id,
        description=reason,
        after_data={"ticket_id": ticket.id, "changed_fields": changed, "apply_status": result_status, "action": action},
    )

    if (
        ticket.current_status_code == "auto_replied"
        and parse_result.intent_type == "new_repair"
        and ticket.source_email_id == email.id
        and not ticket.missing_fields
    ):
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="parsed",
            trigger_event="source_email_reparse_completed",
            user_id=user_id,
            operator_type="user" if user_id else "system",
            reason="Corrected source-email parsing is complete; resume validation.",
            metadata={"parse_result_id": parse_result.id, "email_id": email.id},
        )
    elif ticket.current_status_code == "auto_replied" and parse_result.intent_type in {
        "normal_reply",
        "customer_supplement",
    }:
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="parsed",
            trigger_event="customer_reply_received",
            user_id=user_id,
            operator_type="user" if user_id else "system",
            reason="客户已回复补充信息，重新进入解析校验流程。",
            metadata={"parse_result_id": parse_result.id, "email_id": email.id},
        )
    if (
        ticket.current_status_code == "need_customer_info"
        and parse_result.intent_type in {"new_repair", "customer_supplement"}
        and not ticket.missing_fields
    ):
        await transition_ticket(
            session,
            ticket=ticket,
            to_status_code="parsed",
            trigger_event="customer_info_completed",
            user_id=user_id,
            operator_type="user" if user_id else "system",
            reason="All required customer fields are now complete; resume validation.",
            metadata={"parse_result_id": parse_result.id, "email_id": email.id},
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
    """Run the SN-only first stage without marking the ticket exportable."""
    from app.services.ticket_safety import validate_ticket_sn_core

    result = await validate_ticket_sn_core(session, ticket_id=ticket_id, user_id=user_id)
    detail = await get_ticket_detail(session, ticket_id)
    detail["sn_validation"] = result
    return detail


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
        select(EmailAttachment, Email)
        .join(Email, Email.id == EmailAttachment.email_id)
        .join(EmailTicketLink, EmailTicketLink.email_id == Email.id)
        .where(EmailTicketLink.ticket_id == ticket_id)
        .order_by(EmailAttachment.created_at.desc())
    )
    rows = (await session.execute(statement)).all()
    return [_serialize_attachment_with_email(attachment, email) for attachment, email in rows]


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
