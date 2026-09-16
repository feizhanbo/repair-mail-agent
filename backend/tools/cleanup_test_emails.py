"""按 message_id 列表优雅移除测试邮件及其全量关联记录。

本工具沿 FK 依赖链自上而下逐表解绑 + 删除，覆盖所有引用 emails 和 repair_tickets
的子表（含 IMAP 去重状态 mail_fetch_records 与操作审计日志），确保同批邮件
可在下一轮测试中重新入库。所有 DML 操作在单个 DB 事务中执行，失败自动回滚。

典型用法::

    # 1. dry-run 预览影响
    python tools/cleanup_test_emails.py \
        --message-ids "<202608061022046822681@accotest.com>" \
                       "<202608061026436645613@accotest.com>" \
                       "<202608061036362839674@accotest.com>" \
        --dry-run

    # 2. 实际执行
    python tools/cleanup_test_emails.py \
        --message-ids "<202608061022046822681@accotest.com>" \
                       "<202608061026436645613@accotest.com>" \
                       "<202608061036362839674@accotest.com>"

约束:
    - 不修改 backend/app/ 下任何业务代码
    - 不修改数据库 Schema（无新 Alembic 迁移）
    - 不影响非测试邮件 / 工单数据
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 将 backend/ 加入 sys.path，使 app.* 导入在任意 cwd 下均可用
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import and_, delete, func, or_, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AiCallLog,
    BoardCard,
    CustomerServicePolicy,
    Email,
    EmailAttachment,
    EmailThread,
    EmailTicketLink,
    ExportSap,
    ExternalOperationRecord,
    FieldAuditLog,
    MailFetchRecord,
    ManualReviewTask,
    NotificationEvent,
    OperationLog,
    ParseResult,
    ReplyRecord,
    RepairTicket,
    RepairTicketItem,
    SnAsset,
    SnValidationResult,
    SystemEventLog,
    TicketRelayExport,
    TicketRma,
    TicketRmaItem,
    TicketStatusLog,
)

DEFAULT_MASTER_DATA_BATCH_ID = "real-mail-2026-08-06"


class CleanupError(RuntimeError):
    """清理工具业务错误，用于在事务内主动回滚。"""


# ---------------------------------------------------------------------------
# Step 1: 按 message_id 匹配邮件
# ---------------------------------------------------------------------------
async def match_emails(
    session: AsyncSession, message_ids: list[str]
) -> list[dict[str, Any]]:
    """按 message_id 查询 emails 表，返回匹配摘要（含关联 ticket_no）。"""
    rows = (
        await session.execute(
            select(
                Email.id,
                Email.message_id,
                Email.subject,
                Email.from_address,
                Email.mailbox_account,
                Email.imap_uid,
                Email.thread_id,
            ).where(Email.message_id.in_(message_ids))
        )
    ).all()

    results: list[dict[str, Any]] = []
    for email_id, message_id, subject, from_address, mailbox_account, imap_uid, thread_id in rows:
        # 查关联工单号（source_email_id 或 email_ticket_links）
        ticket_no = await session.scalar(
            select(RepairTicket.ticket_no).where(RepairTicket.source_email_id == email_id)
        )
        if ticket_no is None:
            link_ticket_id = await session.scalar(
                select(EmailTicketLink.ticket_id).where(EmailTicketLink.email_id == email_id)
            )
            if link_ticket_id is not None:
                ticket_no = await session.scalar(
                    select(RepairTicket.ticket_no).where(RepairTicket.id == link_ticket_id)
                )
        results.append(
            {
                "email_id": int(email_id),
                "message_id": message_id,
                "subject": subject,
                "from_address": from_address,
                "mailbox_account": mailbox_account,
                "imap_uid": imap_uid,
                "thread_id": int(thread_id) if thread_id is not None else None,
                "ticket_no": ticket_no,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Step 2: 解析关联工单并分类（delete / unbind）
# ---------------------------------------------------------------------------
async def resolve_ticket_actions(
    session: AsyncSession, email_ids: list[int]
) -> tuple[list[int], list[int]]:
    """返回 (delete_ticket_ids, unbind_ticket_ids)。

    - delete: 工单仅关联目标邮件 → 可安全删除
    - unbind: 工单还关联其他非目标邮件 → 仅解绑 source_email_id / device_received_email_id
    """
    if not email_ids:
        return [], []

    # 通过 source_email_id / device_received_email_id 找工单
    src_rows = (
        await session.execute(
            select(RepairTicket.id).where(
                or_(
                    RepairTicket.source_email_id.in_(email_ids),
                    RepairTicket.device_received_email_id.in_(email_ids),
                )
            )
        )
    ).scalars().all()
    # 通过 email_ticket_links 找工单
    link_rows = (
        await session.execute(
            select(EmailTicketLink.ticket_id).where(EmailTicketLink.email_id.in_(email_ids))
        )
    ).scalars().all()

    candidate_ids = set(int(tid) for tid in src_rows) | set(int(tid) for tid in link_rows)
    delete_ids: list[int] = []
    unbind_ids: list[int] = []
    for ticket_id in candidate_ids:
        # 检查该工单是否还关联其他非目标邮件
        other_link_count = int(
            await session.scalar(
                select(func.count())
                .select_from(EmailTicketLink)
                .where(
                    EmailTicketLink.ticket_id == ticket_id,
                    ~EmailTicketLink.email_id.in_(email_ids),
                )
            )
            or 0
        )
        if other_link_count > 0:
            unbind_ids.append(ticket_id)
        else:
            delete_ids.append(ticket_id)
    delete_ids.sort()
    unbind_ids.sort()
    return delete_ids, unbind_ids


# ---------------------------------------------------------------------------
# Step 3: 逐表删除（FK 安全顺序，单事务）
# ---------------------------------------------------------------------------
async def _delete_rows(
    session: AsyncSession, stmt, *, dry_run: bool, counter: dict[str, int], key: str
) -> None:
    """执行 DELETE/UPDATE 语句并在 counter 中累计影响行数。dry_run 时跳过执行。"""
    if dry_run:
        return
    result = await session.execute(stmt)
    if key not in counter:
        counter[key] = 0
    counter[key] += int(result.rowcount or 0)


async def _handle_email_threads(
    session: AsyncSession,
    email_ids: list[int],
    delete_ticket_ids: list[int],
    *,
    dry_run: bool,
    counter: dict[str, int],
) -> None:
    """防御性处理 email_threads：共享线程仅更新 latest_email_id，独立线程删除。

    同时解除 ticket_id / predecessor_ticket_id 对将被删除工单的引用。
    """
    # 1) 处理 latest_email_id 指向目标邮件的线程
    threads_with_latest = (
        await session.execute(
            select(EmailThread).where(EmailThread.latest_email_id.in_(email_ids))
        )
    ).scalars().all()
    for thread in threads_with_latest:
        # 同线程下是否存在非目标邮件
        other_email_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Email)
                .where(and_(Email.thread_id == thread.id, ~Email.id.in_(email_ids)))
            )
            or 0
        )
        if other_email_count > 0:
            # 共享线程：更新 latest_email_id 为线程中剩余最新邮件
            new_latest = await session.scalar(
                select(Email.id)
                .where(and_(Email.thread_id == thread.id, ~Email.id.in_(email_ids)))
                .order_by(Email.received_at.desc().nullslast())
                .limit(1)
            )
            if not dry_run:
                await session.execute(
                    update(EmailThread)
                    .where(EmailThread.id == thread.id)
                    .values(latest_email_id=new_latest)
                )
            counter.setdefault("email_threads_updated", 0)
            counter["email_threads_updated"] += 1
        else:
            # 独立线程：直接删除
            if not dry_run:
                await session.execute(delete(EmailThread).where(EmailThread.id == thread.id))
            counter.setdefault("email_threads_deleted", 0)
            counter["email_threads_deleted"] += 1

    # 2) 解除对将被删除工单的 ticket_id / predecessor_ticket_id 引用
    if delete_ticket_ids:
        if not dry_run:
            await session.execute(
                update(EmailThread)
                .where(EmailThread.ticket_id.in_(delete_ticket_ids))
                .values(ticket_id=None)
            )
            await session.execute(
                update(EmailThread)
                .where(EmailThread.predecessor_ticket_id.in_(delete_ticket_ids))
                .values(predecessor_ticket_id=None)
            )
        counter.setdefault("email_threads_ticket_unbound", 0)
        counter["email_threads_ticket_unbound"] += 1


async def delete_chain(
    session: AsyncSession,
    email_ids: list[int],
    delete_ticket_ids: list[int],
    unbind_ticket_ids: list[int],
    *,
    dry_run: bool,
) -> dict[str, int]:
    """按 FK 依赖链顺序逐表删除/解绑，返回 per_table 影响行数。

    删除顺序（22 步，覆盖 spec 的 16 步主链 + 审计/集成表）::

        1.  operation_logs            (email_id + ticket_id)
        2.  system_event_logs          (email_id + ticket_id)
        3.  ticket_status_logs        (ticket_id, CASCADE → 显式 DELETE)
        4.  field_audit_logs          (ticket_id + ticket_item_id + parse_result_id, CASCADE → 显式 DELETE)
        5.  external_operation_records(email_id + ticket_id, reply_record_id/export_sap_id CASCADE)
        6.  sn_validation_results     (ticket_id + ticket_item_id CASCADE)
        7.  ticket_rma_items          (via ticket_rma_id 子查询, CASCADE)
        8.  ticket_rmas               (ticket_id CASCADE, reply_record_id FK)
        9.  ticket_relay_exports      (ticket_id CASCADE, 被 export_sap 引用)
        10. export_sap                (ticket_id + ticket_item_id + relay_export_id CASCADE)
        11. repair_ticket_items       (ticket_id CASCADE)
        12. manual_review_tasks       (email_id + ticket_id CASCADE)
        13. notification_events       (ticket_id SET NULL → UPDATE)
        14. reply_records             (related_email_id / outgoing_email_id + ticket_id CASCADE)
        15. ai_call_logs              (email_id + ticket_id, 被 reply_records.ai_call_log_id 引用)
        16. parse_results             (email_id + ticket_id + source_attachment_id)
        17. email_attachments         (email_id)
        18. email_ticket_links        (email_id + ticket_id)
        19. email_threads             (latest_email_id + ticket_id + predecessor_ticket_id, 防御性)
        20. mail_fetch_records        (email_id, IMAP 去重状态)
        21. repair_tickets            (unbind: SET NULL; delete: DELETE)
        22. emails                    (duplicate_of_email_id 自引用 SET NULL, 再 DELETE)
    """
    counter: dict[str, int] = {}
    e_ids = email_ids
    del_t_ids = delete_ticket_ids
    all_t_ids = sorted(set(delete_ticket_ids) | set(unbind_ticket_ids))

    def _in_or_skip(values: list[int]):
        """空列表时返回 False 条件，避免 IN () 语法错误。"""
        return values if values else [-1]

    # 1. operation_logs
    stmt = delete(OperationLog).where(
        or_(
            OperationLog.email_id.in_(_in_or_skip(e_ids)),
            OperationLog.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="operation_logs")

    # 2. system_event_logs
    stmt = delete(SystemEventLog).where(
        or_(
            SystemEventLog.email_id.in_(_in_or_skip(e_ids)),
            SystemEventLog.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="system_event_logs")

    # 3. ticket_status_logs (CASCADE, 显式 DELETE)
    if del_t_ids:
        stmt = delete(TicketStatusLog).where(TicketStatusLog.ticket_id.in_(del_t_ids))
        await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="ticket_status_logs")

    # 4. field_audit_logs (CASCADE, 显式 DELETE; 引用 parse_results 与 repair_ticket_items)
    if del_t_ids:
        stmt = delete(FieldAuditLog).where(FieldAuditLog.ticket_id.in_(del_t_ids))
        await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="field_audit_logs")

    # 5. external_operation_records (email_id SET NULL, ticket_id CASCADE; 引用 reply_records / export_sap)
    stmt = delete(ExternalOperationRecord).where(
        or_(
            ExternalOperationRecord.email_id.in_(_in_or_skip(e_ids)),
            ExternalOperationRecord.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(
        session, stmt, dry_run=dry_run, counter=counter, key="external_operation_records"
    )

    # 6. sn_validation_results (引用 repair_ticket_items CASCADE)
    if del_t_ids:
        stmt = delete(SnValidationResult).where(SnValidationResult.ticket_id.in_(del_t_ids))
        await _delete_rows(
            session, stmt, dry_run=dry_run, counter=counter, key="sn_validation_results"
        )

    # 7. ticket_rma_items (引用 ticket_rmas CASCADE, 通过子查询定位)
    if del_t_ids:
        ticket_rma_id_subquery = select(TicketRma.id).where(TicketRma.ticket_id.in_(del_t_ids))
        stmt = delete(TicketRmaItem).where(TicketRmaItem.ticket_rma_id.in_(ticket_rma_id_subquery))
        await _delete_rows(
            session, stmt, dry_run=dry_run, counter=counter, key="ticket_rma_items"
        )

    # 8. ticket_rmas (ticket_id CASCADE, reply_record_id FK 无 ondelete → 先删 ticket_rmas)
    if del_t_ids:
        stmt = delete(TicketRma).where(TicketRma.ticket_id.in_(del_t_ids))
        await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="ticket_rmas")

    # 9. ticket_relay_exports (ticket_id CASCADE, 被 export_sap 引用)
    if del_t_ids:
        stmt = delete(TicketRelayExport).where(TicketRelayExport.ticket_id.in_(del_t_ids))
        await _delete_rows(
            session, stmt, dry_run=dry_run, counter=counter, key="ticket_relay_exports"
        )

    # 10. export_sap (ticket_id + ticket_item_id + relay_export_id CASCADE)
    if del_t_ids:
        stmt = delete(ExportSap).where(ExportSap.ticket_id.in_(del_t_ids))
        await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="export_sap")

    # 11. repair_ticket_items (ticket_id CASCADE)
    if del_t_ids:
        stmt = delete(RepairTicketItem).where(RepairTicketItem.ticket_id.in_(del_t_ids))
        await _delete_rows(
            session, stmt, dry_run=dry_run, counter=counter, key="repair_ticket_items"
        )

    # 12. manual_review_tasks (email_id + ticket_id CASCADE)
    stmt = delete(ManualReviewTask).where(
        or_(
            ManualReviewTask.email_id.in_(_in_or_skip(e_ids)),
            ManualReviewTask.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(
        session, stmt, dry_run=dry_run, counter=counter, key="manual_review_tasks"
    )

    # 13. notification_events (ticket_id SET NULL → UPDATE)
    if del_t_ids:
        stmt = (
            update(NotificationEvent)
            .where(NotificationEvent.ticket_id.in_(del_t_ids))
            .values(ticket_id=None)
        )
        await _delete_rows(
            session, stmt, dry_run=dry_run, counter=counter, key="notification_events_unbound"
        )

    # 14. reply_records (related_email_id / outgoing_email_id + ticket_id CASCADE)
    stmt = delete(ReplyRecord).where(
        or_(
            ReplyRecord.related_email_id.in_(_in_or_skip(e_ids)),
            ReplyRecord.outgoing_email_id.in_(_in_or_skip(e_ids)),
            ReplyRecord.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="reply_records")

    # 15. ai_call_logs (被 reply_records.ai_call_log_id 引用, 必须在 reply_records 之后)
    stmt = delete(AiCallLog).where(
        or_(
            AiCallLog.email_id.in_(_in_or_skip(e_ids)),
            AiCallLog.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="ai_call_logs")

    # 16. parse_results (email_id + ticket_id + source_attachment_id 引用 email_attachments)
    stmt = delete(ParseResult).where(
        or_(
            ParseResult.email_id.in_(_in_or_skip(e_ids)),
            ParseResult.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="parse_results")

    # 17. email_attachments (email_id; parse_results / ai_call_logs 已删除)
    stmt = delete(EmailAttachment).where(EmailAttachment.email_id.in_(_in_or_skip(e_ids)))
    await _delete_rows(
        session, stmt, dry_run=dry_run, counter=counter, key="email_attachments"
    )

    # 18. email_ticket_links (email_id + ticket_id)
    stmt = delete(EmailTicketLink).where(
        or_(
            EmailTicketLink.email_id.in_(_in_or_skip(e_ids)),
            EmailTicketLink.ticket_id.in_(_in_or_skip(all_t_ids)),
        )
    )
    await _delete_rows(
        session, stmt, dry_run=dry_run, counter=counter, key="email_ticket_links"
    )

    # 19. email_threads (latest_email_id + ticket_id + predecessor_ticket_id, 防御性)
    await _handle_email_threads(
        session, e_ids, del_t_ids, dry_run=dry_run, counter=counter
    )

    # 20. mail_fetch_records (email_id, IMAP 去重状态)
    stmt = delete(MailFetchRecord).where(MailFetchRecord.email_id.in_(_in_or_skip(e_ids)))
    await _delete_rows(
        session, stmt, dry_run=dry_run, counter=counter, key="mail_fetch_records"
    )

    # 21. repair_tickets (unbind: SET NULL; delete: DELETE)
    if unbind_ticket_ids:
        # 仅解绑 source_email_id / device_received_email_id 指向目标邮件的列
        if not dry_run:
            await session.execute(
                update(RepairTicket)
                .where(
                    and_(
                        RepairTicket.id.in_(unbind_ticket_ids),
                        RepairTicket.source_email_id.in_(e_ids),
                    )
                )
                .values(source_email_id=None)
            )
            await session.execute(
                update(RepairTicket)
                .where(
                    and_(
                        RepairTicket.id.in_(unbind_ticket_ids),
                        RepairTicket.device_received_email_id.in_(e_ids),
                    )
                )
                .values(device_received_email_id=None)
            )
        counter.setdefault("repair_tickets_unbound", 0)
        counter["repair_tickets_unbound"] += len(unbind_ticket_ids)

    if del_t_ids:
        stmt = delete(RepairTicket).where(RepairTicket.id.in_(del_t_ids))
        await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="repair_tickets_deleted")

    # 22. emails (duplicate_of_email_id 自引用 → 先 SET NULL, 再 DELETE)
    if e_ids:
        if not dry_run:
            await session.execute(
                update(Email)
                .where(Email.duplicate_of_email_id.in_(e_ids))
                .values(duplicate_of_email_id=None)
            )
        counter.setdefault("emails_duplicate_unbound", 0)
        counter["emails_duplicate_unbound"] += 1

        stmt = delete(Email).where(Email.id.in_(e_ids))
        await _delete_rows(session, stmt, dry_run=dry_run, counter=counter, key="emails_deleted")

    return counter


# ---------------------------------------------------------------------------
# Step 4: 临时主数据清理（复用 run_rmatest_batch_e2e.py 的清理模式）
# ---------------------------------------------------------------------------
async def cleanup_master_data(
    session: AsyncSession, batch_id: str, *, dry_run: bool
) -> dict[str, Any]:
    """清理 source_file_name = batch_id 的临时 master data。

    复用 run_rmatest_batch_e2e.py 中 cleanup_temporary_master_data 的 FK 解绑模式：
    先解除 RepairTicketItem / SnValidationResult / RepairTicket 对临时主数据的引用，
    再删除 SnAsset / BoardCard / CustomerServicePolicy。
    """
    sn_ids = (
        await session.execute(
            select(SnAsset.id).where(SnAsset.source_file_name == batch_id)
        )
    ).scalars().all()
    board_ids = (
        await session.execute(
            select(BoardCard.id).where(BoardCard.source_file_name == batch_id)
        )
    ).scalars().all()
    policy_ids = (
        await session.execute(
            select(CustomerServicePolicy.id).where(
                CustomerServicePolicy.source_file_name == batch_id
            )
        )
    ).scalars().all()

    sn_ids = [int(i) for i in sn_ids]
    board_ids = [int(i) for i in board_ids]
    policy_ids = [int(i) for i in policy_ids]

    if dry_run:
        return {
            "estimated": {
                "sn_assets": len(sn_ids),
                "board_cards": len(board_ids),
                "customer_policies": len(policy_ids),
            },
            "batch_id": batch_id,
        }

    # 解绑 FK 引用（防御性：其他工单可能引用这些临时主数据）
    if sn_ids:
        await session.execute(
            update(SnValidationResult)
            .where(SnValidationResult.matched_sn_asset_id.in_(sn_ids))
            .values(matched_sn_asset_id=None)
        )
        await session.execute(
            update(RepairTicketItem)
            .where(RepairTicketItem.sn_asset_id.in_(sn_ids))
            .values(sn_asset_id=None)
        )
        await session.execute(delete(SnAsset).where(SnAsset.id.in_(sn_ids)))
    if board_ids:
        await session.execute(
            update(RepairTicketItem)
            .where(RepairTicketItem.matched_board_card_id.in_(board_ids))
            .values(matched_board_card_id=None)
        )
        await session.execute(delete(BoardCard).where(BoardCard.id.in_(board_ids)))
    if policy_ids:
        await session.execute(
            update(RepairTicket)
            .where(RepairTicket.service_policy_id.in_(policy_ids))
            .values(service_policy_id=None)
        )
        await session.execute(
            delete(CustomerServicePolicy).where(CustomerServicePolicy.id.in_(policy_ids))
        )

    return {
        "sn_assets_deleted": len(sn_ids),
        "board_cards_deleted": len(board_ids),
        "customer_policies_deleted": len(policy_ids),
        "batch_id": batch_id,
    }


# ---------------------------------------------------------------------------
# Step 5: dry-run 影响行数估算
# ---------------------------------------------------------------------------
async def estimate_counts(
    session: AsyncSession,
    email_ids: list[int],
    delete_ticket_ids: list[int],
    unbind_ticket_ids: list[int],
) -> dict[str, int]:
    """dry-run 模式下估算每张表的影响行数。"""
    estimates: dict[str, int] = {}
    e_ids = email_ids
    del_t_ids = delete_ticket_ids
    all_t_ids = sorted(set(delete_ticket_ids) | set(unbind_ticket_ids))

    async def _count(model, column_predicates) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(column_predicates)
            )
            or 0
        )

    def _in(values):
        return values if values else [-1]

    estimates["operation_logs"] = await _count(
        OperationLog,
        or_(
            OperationLog.email_id.in_(_in(e_ids)),
            OperationLog.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["system_event_logs"] = await _count(
        SystemEventLog,
        or_(
            SystemEventLog.email_id.in_(_in(e_ids)),
            SystemEventLog.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["ticket_status_logs"] = (
        await _count(TicketStatusLog, TicketStatusLog.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["field_audit_logs"] = (
        await _count(FieldAuditLog, FieldAuditLog.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["external_operation_records"] = await _count(
        ExternalOperationRecord,
        or_(
            ExternalOperationRecord.email_id.in_(_in(e_ids)),
            ExternalOperationRecord.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["sn_validation_results"] = (
        await _count(SnValidationResult, SnValidationResult.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["ticket_rmas"] = (
        await _count(TicketRma, TicketRma.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["ticket_relay_exports"] = (
        await _count(TicketRelayExport, TicketRelayExport.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["export_sap"] = (
        await _count(ExportSap, ExportSap.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["repair_ticket_items"] = (
        await _count(RepairTicketItem, RepairTicketItem.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["manual_review_tasks"] = await _count(
        ManualReviewTask,
        or_(
            ManualReviewTask.email_id.in_(_in(e_ids)),
            ManualReviewTask.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["notification_events"] = (
        await _count(NotificationEvent, NotificationEvent.ticket_id.in_(_in(del_t_ids)))
        if del_t_ids
        else 0
    )
    estimates["reply_records"] = await _count(
        ReplyRecord,
        or_(
            ReplyRecord.related_email_id.in_(_in(e_ids)),
            ReplyRecord.outgoing_email_id.in_(_in(e_ids)),
            ReplyRecord.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["ai_call_logs"] = await _count(
        AiCallLog,
        or_(
            AiCallLog.email_id.in_(_in(e_ids)),
            AiCallLog.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["parse_results"] = await _count(
        ParseResult,
        or_(
            ParseResult.email_id.in_(_in(e_ids)),
            ParseResult.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["email_attachments"] = await _count(
        EmailAttachment, EmailAttachment.email_id.in_(_in(e_ids))
    )
    estimates["email_ticket_links"] = await _count(
        EmailTicketLink,
        or_(
            EmailTicketLink.email_id.in_(_in(e_ids)),
            EmailTicketLink.ticket_id.in_(_in(all_t_ids)),
        ),
    )
    estimates["mail_fetch_records"] = await _count(
        MailFetchRecord, MailFetchRecord.email_id.in_(_in(e_ids))
    )
    estimates["emails"] = len(e_ids)
    estimates["repair_tickets_to_delete"] = len(del_t_ids)
    estimates["repair_tickets_to_unbind"] = len(unbind_ticket_ids)
    return estimates


# ---------------------------------------------------------------------------
# Step 6: 删除结果验证
# ---------------------------------------------------------------------------
async def verify_deletion(
    session: AsyncSession, email_ids: list[int], delete_ticket_ids: list[int]
) -> dict[str, Any]:
    """逐表 SELECT COUNT(*) 验证目标记录已不存在，含 IMAP 去重状态。"""
    def _in(values):
        return values if values else [-1]

    async def _count(model, predicate) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(predicate)
            )
            or 0
        )

    results: dict[str, int] = {}
    e_ids = email_ids
    t_ids = delete_ticket_ids

    results["emails"] = await _count(Email, Email.id.in_(_in(e_ids)))
    results["repair_tickets"] = (
        await _count(RepairTicket, RepairTicket.id.in_(_in(t_ids))) if t_ids else 0
    )
    results["email_attachments"] = await _count(
        EmailAttachment, EmailAttachment.email_id.in_(_in(e_ids))
    )
    results["email_ticket_links"] = await _count(
        EmailTicketLink,
        or_(EmailTicketLink.email_id.in_(_in(e_ids)), EmailTicketLink.ticket_id.in_(_in(t_ids))),
    )
    results["parse_results"] = await _count(
        ParseResult,
        or_(ParseResult.email_id.in_(_in(e_ids)), ParseResult.ticket_id.in_(_in(t_ids))),
    )
    results["reply_records"] = await _count(
        ReplyRecord,
        or_(
            ReplyRecord.related_email_id.in_(_in(e_ids)),
            ReplyRecord.outgoing_email_id.in_(_in(e_ids)),
            ReplyRecord.ticket_id.in_(_in(t_ids)),
        ),
    )
    results["manual_review_tasks"] = await _count(
        ManualReviewTask,
        or_(
            ManualReviewTask.email_id.in_(_in(e_ids)),
            ManualReviewTask.ticket_id.in_(_in(t_ids)),
        ),
    )
    results["ai_call_logs"] = await _count(
        AiCallLog,
        or_(AiCallLog.email_id.in_(_in(e_ids)), AiCallLog.ticket_id.in_(_in(t_ids))),
    )
    results["operation_logs"] = await _count(
        OperationLog,
        or_(OperationLog.email_id.in_(_in(e_ids)), OperationLog.ticket_id.in_(_in(t_ids))),
    )
    results["system_event_logs"] = await _count(
        SystemEventLog,
        or_(SystemEventLog.email_id.in_(_in(e_ids)), SystemEventLog.ticket_id.in_(_in(t_ids))),
    )
    results["external_operation_records"] = await _count(
        ExternalOperationRecord,
        or_(
            ExternalOperationRecord.email_id.in_(_in(e_ids)),
            ExternalOperationRecord.ticket_id.in_(_in(t_ids)),
        ),
    )
    results["sn_validation_results"] = (
        await _count(SnValidationResult, SnValidationResult.ticket_id.in_(_in(t_ids)))
        if t_ids
        else 0
    )
    results["ticket_rmas"] = (
        await _count(TicketRma, TicketRma.ticket_id.in_(_in(t_ids))) if t_ids else 0
    )
    results["repair_ticket_items"] = (
        await _count(RepairTicketItem, RepairTicketItem.ticket_id.in_(_in(t_ids)))
        if t_ids
        else 0
    )
    results["ticket_status_logs"] = (
        await _count(TicketStatusLog, TicketStatusLog.ticket_id.in_(_in(t_ids)))
        if t_ids
        else 0
    )
    results["field_audit_logs"] = (
        await _count(FieldAuditLog, FieldAuditLog.ticket_id.in_(_in(t_ids)))
        if t_ids
        else 0
    )
    results["ticket_relay_exports"] = (
        await _count(TicketRelayExport, TicketRelayExport.ticket_id.in_(_in(t_ids)))
        if t_ids
        else 0
    )
    results["export_sap"] = (
        await _count(ExportSap, ExportSap.ticket_id.in_(_in(t_ids))) if t_ids else 0
    )
    # email_threads: 检查 latest_email_id / ticket_id / predecessor_ticket_id
    results["email_threads_latest_email"] = await _count(
        EmailThread, EmailThread.latest_email_id.in_(_in(e_ids))
    )
    results["email_threads_ticket"] = (
        await _count(EmailThread, EmailThread.ticket_id.in_(_in(t_ids))) if t_ids else 0
    )
    # IMAP 去重状态（关键）
    results["mail_fetch_records"] = await _count(
        MailFetchRecord, MailFetchRecord.email_id.in_(_in(e_ids))
    )
    # notification_events: ticket_id 应为 NULL
    results["notification_events_still_bound"] = (
        await _count(NotificationEvent, NotificationEvent.ticket_id.in_(_in(t_ids)))
        if t_ids
        else 0
    )

    all_zero = all(value == 0 for value in results.values())
    return {
        "per_table": results,
        "all_zero": all_zero,
        "imap_dedup_cleared": results.get("mail_fetch_records", -1) == 0,
    }


# ---------------------------------------------------------------------------
# Step 7: 主流程
# ---------------------------------------------------------------------------
async def run_cleanup(
    message_ids: list[str],
    *,
    dry_run: bool,
    master_data_batch_id: str,
    skip_master_data: bool,
) -> dict[str, Any]:
    """主流程：匹配 → 解析工单 → 删除链 → 主数据清理 → 验证。"""
    summary: dict[str, Any] = {
        "requested_message_ids": message_ids,
        "matched_emails": [],
        "matched_email_ids": [],
        "matched_ticket_nos": [],
        "ticket_actions": {"delete": [], "unbind": []},
        "per_table_counts": {},
        "master_data_cleanup": None,
        "verification": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "exit_code": 0,
        "error": None,
    }

    async with AsyncSessionLocal() as session:
        try:
            # Phase 1: 匹配邮件
            matched = await match_emails(session, message_ids)
            if not matched:
                missing = sorted(set(message_ids))
                summary["error"] = f"未找到匹配邮件: {missing}"
                summary["exit_code"] = 1
                return summary
            if len(matched) < len(message_ids):
                found_ids = {m["message_id"] for m in matched}
                missing = sorted(set(message_ids) - found_ids)
                summary["error"] = f"未找到匹配邮件: {missing}"
                summary["exit_code"] = 1
                return summary

            summary["matched_emails"] = matched
            summary["matched_email_ids"] = [m["email_id"] for m in matched]
            summary["matched_ticket_nos"] = [
                m["ticket_no"] for m in matched if m["ticket_no"]
            ]

            email_ids = [m["email_id"] for m in matched]

            # Phase 2: 解析工单动作
            delete_ticket_ids, unbind_ticket_ids = await resolve_ticket_actions(
                session, email_ids
            )
            summary["ticket_actions"]["delete"] = delete_ticket_ids
            summary["ticket_actions"]["unbind"] = unbind_ticket_ids

            if dry_run:
                # dry-run: 估算影响行数，不执行 DML
                estimates = await estimate_counts(
                    session, email_ids, delete_ticket_ids, unbind_ticket_ids
                )
                summary["per_table_counts"] = {"estimated": estimates}
                if not skip_master_data:
                    summary["master_data_cleanup"] = await cleanup_master_data(
                        session, master_data_batch_id, dry_run=True
                    )
            else:
                # Phase 3: 执行删除链（单事务）
                per_table = await delete_chain(
                    session,
                    email_ids,
                    delete_ticket_ids,
                    unbind_ticket_ids,
                    dry_run=False,
                )
                summary["per_table_counts"] = per_table

                # Phase 4: 临时主数据清理（同一事务内）
                if not skip_master_data:
                    summary["master_data_cleanup"] = await cleanup_master_data(
                        session, master_data_batch_id, dry_run=False
                    )

                await session.commit()
        except Exception as exc:
            await session.rollback()
            summary["exit_code"] = 1
            summary["error"] = f"{type(exc).__name__}: {exc}"
            return summary

    # Phase 5: 验证（新 session，读取已提交状态）
    if not dry_run:
        async with AsyncSessionLocal() as verify_session:
            summary["verification"] = await verify_deletion(
                verify_session, email_ids, delete_ticket_ids
            )
            if not summary["verification"].get("all_zero"):
                summary["exit_code"] = 2  # 删除成功但验证发现残留

    return summary


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 message_id 列表优雅移除测试邮件及其全量关联记录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            '  python tools/cleanup_test_emails.py --message-ids "<id1>" "<id2>" --dry-run\n'
            '  python tools/cleanup_test_emails.py --message-ids "<id1>" "<id2>"\n'
            '  python tools/cleanup_test_emails.py --message-ids "<id1>" --skip-master-data\n'
        ),
    )
    parser.add_argument(
        "--message-ids",
        nargs="+",
        required=True,
        help="目标邮件的 message_id 列表（必填，至少 1 个）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览影响行数，不执行实际删除/更新",
    )
    parser.add_argument(
        "--master-data-batch-id",
        default=DEFAULT_MASTER_DATA_BATCH_ID,
        help=f"临时主数据 source_file_name 过滤值（默认 {DEFAULT_MASTER_DATA_BATCH_ID}）",
    )
    parser.add_argument(
        "--skip-master-data",
        action="store_true",
        help="跳过临时主数据清理（sn_assets / board_cards / customer_service_policies）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        f"[cleanup] 启动 dry_run={args.dry_run} message_ids={args.message_ids}",
        file=sys.stderr,
    )
    summary = asyncio.run(
        run_cleanup(
            args.message_ids,
            dry_run=args.dry_run,
            master_data_batch_id=args.master_data_batch_id,
            skip_master_data=args.skip_master_data,
        )
    )

    # 打印匹配摘要到 stderr（人类可读）
    if summary.get("matched_emails"):
        print("\n[matched emails]", file=sys.stderr)
        for m in summary["matched_emails"]:
            print(
                f"  email_id={m['email_id']} message_id={m['message_id']} "
                f"subject={m['subject']!r} ticket_no={m['ticket_no']}",
                file=sys.stderr,
            )
        print(
            f"\n[ticket actions] delete={summary['ticket_actions']['delete']} "
            f"unbind={summary['ticket_actions']['unbind']}",
            file=sys.stderr,
        )

    if args.dry_run and summary.get("per_table_counts", {}).get("estimated"):
        print("\n[estimated impact]", file=sys.stderr)
        for table, count in summary["per_table_counts"]["estimated"].items():
            print(f"  {table}: {count}", file=sys.stderr)

    if summary.get("master_data_cleanup"):
        md = summary["master_data_cleanup"]
        if md.get("estimated"):
            print(
                f"\n[master data] estimated sn_assets={md['estimated']['sn_assets']} "
                f"board_cards={md['estimated']['board_cards']} "
                f"customer_policies={md['estimated']['customer_policies']}",
                file=sys.stderr,
            )
        else:
            print(
                f"\n[master data] deleted sn_assets={md.get('sn_assets_deleted', 0)} "
                f"board_cards={md.get('board_cards_deleted', 0)} "
                f"customer_policies={md.get('customer_policies_deleted', 0)}",
                file=sys.stderr,
            )

    if summary.get("verification"):
        v = summary["verification"]
        print(
            f"\n[verification] all_zero={v['all_zero']} "
            f"imap_dedup_cleared={v['imap_dedup_cleared']}",
            file=sys.stderr,
        )
        if not v["all_zero"]:
            print("  RESIDUAL ROWS DETECTED:", file=sys.stderr)
            for table, count in v["per_table"].items():
                if count > 0:
                    print(f"    {table}: {count}", file=sys.stderr)

    if summary.get("error"):
        print(f"\n[error] {summary['error']}", file=sys.stderr)

    # 完整 JSON 摘要到 stdout（机器可读）
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    return int(summary.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
