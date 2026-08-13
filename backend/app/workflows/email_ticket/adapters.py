from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Email, EmailAttachment, EmailThread, ManualReviewTask, ParseResult, RepairTicket
from app.services.replies import create_reply_draft, prepare_rma_authorization, retry_rma_archive, send_prepared_reply, send_prepared_rma_authorization
from app.services.emails import adopt_email_parse_context, generate_email_ai_candidate, prepare_email_parse_context
from app.services.audit import log_system_event
from app.services.rma_pdf import TEMPLATE_VERSION as RMA_TEMPLATE_VERSION
from app.services.sap_rma import poll_export_batch, reconcile_uncertain_submission, submit_export_batch
from app.services.ticket_safety import validate_and_mark_ready_for_export
from app.services.tickets import patch_ticket_fields
from app.services.workflow import OPEN_TASK_STATUSES, create_email_manual_task_if_missing, create_manual_task_if_missing, transition_ticket


T = TypeVar("T")


class ReadOnlyEmailSnapshotLoader:
    """Load small JSON-safe workflow facts without mutating ORM entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __call__(self, email_id: int) -> dict[str, Any]:
        email = await self.session.get(Email, email_id)
        if email is None:
            raise LookupError("EMAIL_NOT_FOUND")
        thread = await self.session.get(EmailThread, email.thread_id) if email.thread_id else None
        ticket = await self.session.get(RepairTicket, thread.ticket_id) if thread and thread.ticket_id else None
        attachments = (
            await self.session.execute(
                select(EmailAttachment).where(EmailAttachment.email_id == email.id).order_by(EmailAttachment.id)
            )
        ).scalars().all()
        parse_result = (
            await self.session.execute(
                select(ParseResult)
                .where(ParseResult.email_id == email.id)
                .order_by(ParseResult.created_at.desc(), ParseResult.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "id": email.id,
            "thread_id": email.thread_id,
            "ticket_id": ticket.id if ticket else None,
            "ticket_status": ticket.current_status_code if ticket else None,
            "ticket_version": ticket.version if ticket else None,
            "subject": email.subject,
            "text_body": email.text_body,
            "clean_body": email.clean_body,
            "latest_reply_segment": email.latest_reply_segment,
            "in_reply_to": email.in_reply_to,
            "references_header": email.references_header,
            "intent_type": email.intent_type,
            "intent_subtype": email.intent_subtype,
            "handling_level": email.handling_level,
            "classification_confidence": _number(email.classification_confidence),
            "attachments": [_attachment_snapshot(item) for item in attachments],
            "parse_result": _parse_snapshot(parse_result),
        }


def _attachment_snapshot(attachment: EmailAttachment) -> dict[str, Any]:
    extracted = attachment.extracted_json or {}
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "file_type": extracted.get("file_type"),
        "parse_status": attachment.parse_status,
        "parse_error": attachment.parse_error,
        "blocks_ticket_flow": extracted.get("blocks_ticket_flow", False),
        "warnings": extracted.get("warnings") or extracted.get("security_warnings") or [],
    }


def _parse_snapshot(result: ParseResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "id": result.id,
        "parser_type": result.parser_type,
        "intent_type": result.intent_type,
        "intent_subtype": result.intent_subtype,
        "handling_level": result.handling_level,
        "classification_confidence": _number(result.classification_confidence),
        "confidence_score": _number(result.confidence_score),
        "missing_fields": result.missing_fields or {},
        "conflict_fields": result.conflict_fields or {},
        "extracted_fields": result.extracted_fields or {},
        "extracted_items": result.extracted_items or [],
    }


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def normalize_reply_prepare_result(result: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize create_reply_draft output for the workflow router.

    create_reply_draft returns either a brand-new draft dict (``reply_id`` plus
    ``status``) or a flat ``serialize_reply`` of a reused draft (``id`` plus
    ``send_status``).  The Graph contract consumes only the canonical fields:
    ``status == "prepared"``, ``reply_id`` and ``send_status``.
    """
    return {
        "status": "prepared",
        "reply_id": _reply_prepare_reply_id(result),
        "send_status": _reply_prepare_send_status(result),
        "reply": result,
    }


def _reply_prepare_reply_id(result: dict[str, Any]) -> Any:
    reply_id = result.get("reply_id")
    if reply_id is not None:
        return reply_id
    reply_id = result.get("id")
    if reply_id is not None:
        return reply_id
    nested = result.get("reply")
    if isinstance(nested, dict):
        return nested.get("id")
    return None


def _reply_prepare_send_status(result: dict[str, Any]) -> str:
    send_status = result.get("send_status")
    if send_status:
        return str(send_status)
    status = str(result.get("status") or "unknown")
    # A brand-new draft is only returned as status="prepared" when auto-send is
    # enabled; the persisted ReplyRecord then carries approved_pending_send.
    if status == "prepared":
        return "approved_pending_send"
    return status


class ActiveWorkflowServices:
    """Thin LangGraph adapters over existing deterministic business services."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _durable(self, operation: Awaitable[T]) -> T:
        """Commit MySQL business facts before LangGraph checkpoints the node."""
        try:
            result = await operation
            await self.session.commit()
            return result
        except Exception:
            await self.session.rollback()
            raise

    async def record_node_event(self, event: dict[str, Any]) -> None:
        try:
            status = str(event.get("status") or "unknown")
            await log_system_event(
                self.session,
                event_type="langgraph_node_execution",
                module_name="email_ticket_workflow",
                message=f"LangGraph node {event.get('node')} {status}",
                severity="error" if status == "failed" else "info",
                correlation_id=event.get("execution_id"),
                email_id=event.get("email_id"),
                ticket_id=event.get("ticket_id"),
                event_stage=str(event.get("node") or "unknown")[:50],
                event_status=status[:30],
                target_type="workflow_execution",
                duration_ms=event.get("duration_ms"),
                error_code=event.get("error_code"),
                details={
                    "graph_thread_id": event.get("graph_thread_id"),
                    "route_delta": event.get("route_delta") or [],
                },
            )
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

    async def prepare_email_parse(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._durable(prepare_email_parse_context(
            self.session,
            email_id=int(request["email_id"]),
            user_id=request.get("user_id"),
            reason=str(request.get("reason") or "LangGraph email parse"),
            durable_attachment_stages=bool(request.get("durable_attachment_stages")),
            rule_parse_result_id=request.get("rule_parse_result_id"),
            execution_id=request.get("execution_id"),
        ))

    async def generate_ai_candidate(self, context: dict[str, Any]) -> dict[str, Any]:
        return await self._durable(generate_email_ai_candidate(self.session, context=context))

    async def adopt_email_candidate(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._durable(adopt_email_parse_context(
            self.session,
            context=dict(request["parse_context"]),
            ai_candidate=dict(request["ai_candidate"]),
            user_id=request.get("user_id"),
            reason=str(request.get("reason") or "LangGraph candidate adoption"),
            orchestrate_downstream=False,
        ))

    async def apply_human_decision(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._durable(self._apply_human_decision(request))

    async def _apply_human_decision(self, request: dict[str, Any]) -> dict[str, Any]:
        edited_fields = dict(request.get("edited_fields") or {})
        ticket_id = request.get("ticket_id")
        reviewer_id = request.get("reviewer_id")
        if edited_fields:
            if ticket_id is None or reviewer_id is None:
                raise ValueError("HUMAN_EDIT_CONTEXT_REQUIRED")
            await patch_ticket_fields(
                self.session,
                ticket_id=int(ticket_id),
                fields=edited_fields,
                user_id=int(reviewer_id),
                reason="LangGraph human review correction",
                version=request.get("expected_ticket_version"),
            )
        action = str(request.get("action") or "")
        next_action = str(request.get("next_action") or "")
        if ticket_id is not None and action == "request_customer_info":
            ticket = await self.session.get(RepairTicket, int(ticket_id), with_for_update=True)
            if ticket is None:
                raise LookupError("TICKET_NOT_FOUND")
            if ticket.current_status_code in {"manual_review", "parsed"}:
                await transition_ticket(
                    self.session,
                    ticket=ticket,
                    to_status_code="need_customer_info",
                    trigger_event="manual_resolved" if ticket.current_status_code == "manual_review" else "missing_fields_detected",
                    user_id=request.get("reviewer_id"),
                    reason="Human review requested customer information",
                    resolving_task_id=request.get("task_id") if ticket.current_status_code == "manual_review" else None,
                )
        elif ticket_id is not None and action == "close" and next_action in {"resolve_manual_business", "finish_external_handling"}:
            ticket = await self.session.get(RepairTicket, int(ticket_id), with_for_update=True)
            if ticket is not None and ticket.ticket_category == "manual_business" and ticket.current_status_code != "resolved":
                await transition_ticket(
                    self.session,
                    ticket=ticket,
                    to_status_code="resolved",
                    trigger_event="manual_business_resolved",
                    user_id=request.get("reviewer_id"),
                    reason="Human review completed external handling",
                    resolving_task_id=request.get("task_id"),
                )
        result = {
            "status": "applied",
            "action": action,
            "edited": bool(edited_fields),
        }
        return result

    async def validate_ticket(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._durable(validate_and_mark_ready_for_export(
            self.session,
            ticket_id=int(request["ticket_id"]),
            user_id=None,
            resolving_task_id=request.get("resolving_task_id"),
            enqueue_relay_job=False,
        ))

    async def submit_sap(self, export_id: int) -> dict[str, Any]:
        return await self._durable(submit_export_batch(self.session, export_id=export_id, schedule_jobs=False))

    async def reconcile_sap(self, export_id: int) -> dict[str, Any]:
        return await self._durable(reconcile_uncertain_submission(
            self.session,
            export_id=export_id,
            reason="langgraph_uncertain_submit_reconciliation",
            user_id=None,
            schedule_jobs=False,
        ))

    async def poll_sap(self, export_id: int) -> dict[str, Any]:
        return await self._durable(poll_export_batch(
            self.session,
            export_id=export_id,
            schedule_jobs=False,
            enqueue_rma_job=False,
        ))

    async def prepare_rma(self, request: dict[str, Any]) -> dict[str, Any]:
        ticket = await self.session.get(RepairTicket, int(request["ticket_id"]))
        if ticket is None:
            raise LookupError("TICKET_NOT_FOUND")
        return await self._durable(prepare_rma_authorization(
            self.session,
            ticket_id=ticket.id,
            user_id=None,
            expected_version=ticket.version,
            expected_safety_hash=ticket.safety_check_hash or "",
            expected_sn_validation_hash=ticket.sn_validation_hash or "",
            expected_rma_template_version=RMA_TEMPLATE_VERSION,
            expected_rma_no=str(request.get("rma_no") or ""),
        ))

    async def send_rma(self, reply_id: int) -> dict[str, Any]:
        return await self._durable(send_prepared_rma_authorization(
            self.session,
            reply_id=reply_id,
            user_id=None,
            auto=True,
        ))

    async def finalize_rma_archive(self, reply_id: int) -> dict[str, Any]:
        return await self._durable(retry_rma_archive(self.session, reply_id=reply_id, user_id=None))

    async def prepare_reply(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._durable(self._prepare_reply(request))

    async def _prepare_reply(self, request: dict[str, Any]) -> dict[str, Any]:
        result = await create_reply_draft(
            self.session,
            ticket_id=int(request["ticket_id"]),
            user_id=None,
            reply_type=str(request["reply_type"]),
            related_email_id=int(request["email_id"]),
            missing_fields=request.get("missing_fields"),
            send_immediately=False,
        )
        return normalize_reply_prepare_result(result)

    async def send_reply(self, reply_id: int) -> dict[str, Any]:
        return await self._durable(send_prepared_reply(
            self.session,
            reply_id=reply_id,
            user_id=None,
            auto=True,
        ))

    async def create_human_task(self, request: dict[str, Any]) -> int:
        return await self._durable(self._create_human_task(request))

    async def _create_human_task(self, request: dict[str, Any]) -> int:
        ticket_id = request.get("ticket_id")
        reasons = ",".join(str(item) for item in request.get("reasons") or ["MANUAL_REVIEW_REQUIRED"])
        if request.get("reply_id") is not None:
            task_types = (
                {"rma_reply_review", "rma_attachment_disabled"}
                if request.get("review_type") == "rma_reply"
                else {"reply_review"}
            )
            existing = await self.session.scalar(
                select(ManualReviewTask)
                .where(
                    ManualReviewTask.ticket_id == ticket_id,
                    ManualReviewTask.task_type.in_(task_types),
                    ManualReviewTask.status.in_(OPEN_TASK_STATUSES),
                )
                .order_by(ManualReviewTask.id.desc())
            )
            if existing is not None:
                return existing.id
        if ticket_id is not None:
            ticket = await self.session.get(RepairTicket, int(ticket_id))
            if ticket is None:
                raise LookupError("TICKET_NOT_FOUND")
            task = await create_manual_task_if_missing(
                self.session,
                ticket=ticket,
                task_type="langgraph_human_review",
                trigger_reason=reasons,
                email_id=request.get("email_id"),
                recovery_stage="langgraph_interrupt",
                recovery_action="Resume the bound workflow execution after structured review.",
            )
            return task.id
        email = await self.session.get(Email, int(request["email_id"]))
        if email is None:
            raise LookupError("EMAIL_NOT_FOUND")
        task = await create_email_manual_task_if_missing(
            self.session,
            email=email,
            task_type="langgraph_human_review",
            trigger_reason=reasons,
            recovery_stage="langgraph_interrupt",
            recovery_action="Resume the bound workflow execution after structured review.",
        )
        return task.id
