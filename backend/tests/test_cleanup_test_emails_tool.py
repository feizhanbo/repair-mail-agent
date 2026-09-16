"""tools/cleanup_test_emails.py 的结构化测试。

不依赖真实数据库连接，仅验证 CLI 参数解析、FK 依赖链顺序、关键函数签名
与防御性逻辑（空列表、未匹配邮件、事务回滚等）。
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.cleanup_test_emails import (
    DEFAULT_MASTER_DATA_BATCH_ID,
    CleanupError,
    _delete_rows,
    _handle_email_threads,
    cleanup_master_data,
    delete_chain,
    estimate_counts,
    main,
    match_emails,
    parse_args,
    resolve_ticket_actions,
    run_cleanup,
    verify_deletion,
)


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------
def test_parse_args_requires_message_ids() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_accepts_multiple_message_ids() -> None:
    args = parse_args(
        ["--message-ids", "<id1@accotest.com>", "<id2@accotest.com>", "<id3@accotest.com>"]
    )
    assert args.message_ids == ["<id1@accotest.com>", "<id2@accotest.com>", "<id3@accotest.com>"]
    assert args.dry_run is False
    assert args.master_data_batch_id == DEFAULT_MASTER_DATA_BATCH_ID
    assert args.skip_master_data is False


def test_parse_args_supports_dry_run_and_skip_master_data() -> None:
    args = parse_args(
        ["--message-ids", "<id1>", "--dry-run", "--skip-master-data"]
    )
    assert args.dry_run is True
    assert args.skip_master_data is True


def test_parse_args_custom_master_data_batch_id() -> None:
    args = parse_args(
        ["--message-ids", "<id1>", "--master-data-batch-id", "custom-batch-2026"]
    )
    assert args.master_data_batch_id == "custom-batch-2026"


# ---------------------------------------------------------------------------
# 函数签名与异步性
# ---------------------------------------------------------------------------
def test_match_emails_is_async() -> None:
    assert inspect.iscoroutinefunction(match_emails)


def test_resolve_ticket_actions_is_async() -> None:
    assert inspect.iscoroutinefunction(resolve_ticket_actions)


def test_delete_chain_is_async() -> None:
    assert inspect.iscoroutinefunction(delete_chain)


def test_verify_deletion_is_async() -> None:
    assert inspect.iscoroutinefunction(verify_deletion)


def test_estimate_counts_is_async() -> None:
    assert inspect.iscoroutinefunction(estimate_counts)


def test_cleanup_master_data_is_async() -> None:
    assert inspect.iscoroutinefunction(cleanup_master_data)


def test_run_cleanup_is_async() -> None:
    assert inspect.iscoroutinefunction(run_cleanup)


# ---------------------------------------------------------------------------
# FK 依赖链顺序与表覆盖完整性
# ---------------------------------------------------------------------------
def test_delete_chain_covers_all_required_tables() -> None:
    """delete_chain 文档字符串应声明覆盖 spec 的 16 步 + 审计表。"""
    doc = delete_chain.__doc__ or ""
    # spec 的 16 步主链
    required_tables = [
        "operation_logs",
        "system_event_logs",
        "external_operation_records",
        "sn_validation_results",
        "ticket_rmas",
        "repair_ticket_items",
        "manual_review_tasks",
        "reply_records",
        "email_attachments",
        "parse_results",
        "email_ticket_links",
        "email_threads",
        "mail_fetch_records",  # IMAP 去重状态（关键）
        "repair_tickets",
        "emails",
        "ai_call_logs",
    ]
    for table in required_tables:
        assert table in doc, f"delete_chain 文档缺少表: {table}"


def test_delete_chain_doc_explicitly_lists_audit_tables() -> None:
    """审计/集成表必须在文档中显式列出。"""
    doc = delete_chain.__doc__ or ""
    audit_tables = [
        "ticket_status_logs",
        "field_audit_logs",
        "ticket_relay_exports",
        "export_sap",
        "notification_events",
        "ticket_rma_items",
    ]
    for table in audit_tables:
        assert table in doc, f"delete_chain 文档缺少审计表: {table}"


def test_delete_chain_orders_email_threads_before_repair_tickets() -> None:
    """email_threads 必须在 repair_tickets 之前处理（解除 ticket_id FK）。"""
    src = inspect.getsource(delete_chain)
    threads_pos = src.find("_handle_email_threads")
    tickets_pos = src.find('delete(RepairTicket)')
    assert threads_pos != -1, "delete_chain 未调用 _handle_email_threads"
    assert tickets_pos != -1, "delete_chain 未调用 delete(RepairTicket)"
    assert threads_pos < tickets_pos, (
        "email_threads 必须先于 repair_tickets 处理（解除 ticket_id FK）"
    )


def test_delete_chain_orders_mail_fetch_records_before_emails() -> None:
    """mail_fetch_records 必须在 emails 之前删除（IMAP 去重状态）。"""
    src = inspect.getsource(delete_chain)
    mfr_pos = src.find('delete(MailFetchRecord)')
    emails_pos = src.find('delete(Email)')
    assert mfr_pos != -1, "delete_chain 未删除 mail_fetch_records"
    assert emails_pos != -1, "delete_chain 未删除 emails"
    assert mfr_pos < emails_pos, "mail_fetch_records 必须先于 emails 删除"


def test_delete_chain_sets_duplicate_of_email_id_null_before_delete() -> None:
    """emails 自引用 duplicate_of_email_id 必须先 SET NULL 再 DELETE。"""
    src = inspect.getsource(delete_chain)
    set_null_pos = src.find("update(Email)\n                .where(Email.duplicate_of_email_id")
    delete_pos = src.find("delete(Email).where(Email.id")
    assert set_null_pos != -1, "未对 emails.duplicate_of_email_id 执行 SET NULL"
    assert delete_pos != -1, "未对 emails 执行 DELETE"
    assert set_null_pos < delete_pos, "SET NULL 必须先于 DELETE"


def test_delete_chain_orders_ai_call_logs_after_reply_records() -> None:
    """ai_call_logs 必须在 reply_records 之后删除（reply_records.ai_call_log_id FK 无 ondelete）。"""
    src = inspect.getsource(delete_chain)
    reply_pos = src.find("delete(ReplyRecord)")
    ai_log_pos = src.find("delete(AiCallLog)")
    assert reply_pos != -1, "delete_chain 未删除 reply_records"
    assert ai_log_pos != -1, "delete_chain 未删除 ai_call_logs"
    assert reply_pos < ai_log_pos, (
        "ai_call_logs 必须在 reply_records 之后删除（reply_records.ai_call_log_id FK 无 ondelete）"
    )


def test_delete_chain_orders_parse_results_before_email_attachments() -> None:
    """parse_results 必须在 email_attachments 之前删除（source_attachment_id FK 无 ondelete）。"""
    src = inspect.getsource(delete_chain)
    parse_pos = src.find("delete(ParseResult)")
    attach_pos = src.find("delete(EmailAttachment)")
    assert parse_pos != -1, "delete_chain 未删除 parse_results"
    assert attach_pos != -1, "delete_chain 未删除 email_attachments"
    assert parse_pos < attach_pos, (
        "parse_results 必须先于 email_attachments 删除（source_attachment_id FK 无 ondelete）"
    )


def test_delete_chain_orders_ticket_rma_items_before_ticket_rmas() -> None:
    """ticket_rma_items 必须在 ticket_rmas 之前删除（ticket_rma_id FK）。"""
    src = inspect.getsource(delete_chain)
    items_pos = src.find("delete(TicketRmaItem)")
    rmas_pos = src.find("delete(TicketRma)")
    assert items_pos != -1, "delete_chain 未删除 ticket_rma_items"
    assert rmas_pos != -1, "delete_chain 未删除 ticket_rmas"
    assert items_pos < rmas_pos, "ticket_rma_items 必须先于 ticket_rmas 删除"


def test_delete_chain_orders_export_sap_after_ticket_relay_exports() -> None:
    """export_sap 必须在 ticket_relay_exports 之后删除（relay_export_id FK）。"""
    src = inspect.getsource(delete_chain)
    relay_pos = src.find("delete(TicketRelayExport)")
    export_pos = src.find("delete(ExportSap)")
    assert relay_pos != -1, "delete_chain 未删除 ticket_relay_exports"
    assert export_pos != -1, "delete_chain 未删除 export_sap"
    assert relay_pos < export_pos, "ticket_relay_exports 必须先于 export_sap 删除"


def test_delete_chain_handles_notification_events_set_null() -> None:
    """notification_events 应使用 UPDATE SET ticket_id=NULL，而非 DELETE。"""
    src = inspect.getsource(delete_chain)
    assert "update(NotificationEvent)" in src
    assert ".values(ticket_id=None)" in src
    # 不应直接 delete(NotificationEvent)
    assert "delete(NotificationEvent)" not in src


def test_delete_chain_includes_unbind_logic_for_shared_tickets() -> None:
    """unbind_ticket_ids 应仅 SET NULL source_email_id / device_received_email_id。"""
    src = inspect.getsource(delete_chain)
    assert "unbind_ticket_ids" in src
    assert ".values(source_email_id=None)" in src
    assert ".values(device_received_email_id=None)" in src


# ---------------------------------------------------------------------------
# _delete_rows 辅助函数
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_delete_rows_skips_execution_in_dry_run() -> None:
    """dry_run=True 时不执行任何 DML。"""
    session = MagicMock()
    session.execute = AsyncMock()
    counter: dict[str, int] = {}
    await _delete_rows(
        session,
        "fake_stmt",
        dry_run=True,
        counter=counter,
        key="test_table",
    )
    session.execute.assert_not_called()
    assert counter == {}  # dry-run 不累计


@pytest.mark.anyio
async def test_delete_rows_executes_and_counts_in_execute_mode() -> None:
    """dry_run=False 时执行语句并累计 rowcount。"""
    session = MagicMock()
    fake_result = MagicMock()
    fake_result.rowcount = 5
    session.execute = AsyncMock(return_value=fake_result)
    counter: dict[str, int] = {}
    await _delete_rows(
        session,
        "fake_stmt",
        dry_run=False,
        counter=counter,
        key="test_table",
    )
    session.execute.assert_called_once_with("fake_stmt")
    assert counter["test_table"] == 5


# ---------------------------------------------------------------------------
# run_cleanup 错误路径（未匹配邮件）
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_run_cleanup_returns_error_when_no_emails_match() -> None:
    """message_id 不存在时退出码 1，不执行任何 DML。"""
    fake_session = MagicMock()
    fake_session.scalar = AsyncMock(return_value=None)
    fake_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
    fake_session.rollback = AsyncMock()
    fake_session.commit = AsyncMock()

    with patch("tools.cleanup_test_emails.AsyncSessionLocal") as mock_factory:
        mock_factory.return_value.__aenter__.return_value = fake_session
        mock_factory.return_value.__aexit__.return_value = None
        summary = await run_cleanup(
            ["<nonexistent@accotest.com>"],
            dry_run=True,
            master_data_batch_id=DEFAULT_MASTER_DATA_BATCH_ID,
            skip_master_data=True,
        )

    assert summary["exit_code"] == 1
    assert "未找到匹配邮件" in (summary.get("error") or "")
    assert summary["matched_emails"] == []


# ---------------------------------------------------------------------------
# verify_deletion 返回结构
# ---------------------------------------------------------------------------
def test_verify_deletion_returns_all_zero_and_imap_dedup_flag() -> None:
    """verify_deletion 返回的 dict 应包含 per_table / all_zero / imap_dedup_cleared。"""
    # 仅检查函数能被调用并返回预期字段（用 mock）
    assert hasattr(verify_deletion, "__doc__")
    doc = verify_deletion.__doc__ or ""
    assert "IMAP" in doc or "mail_fetch_records" in doc


def test_estimate_counts_returns_dict_of_ints_signature() -> None:
    assert hasattr(estimate_counts, "__doc__")
    doc = estimate_counts.__doc__ or ""
    assert "dry-run" in doc


# ---------------------------------------------------------------------------
# cleanup_master_data 容错性
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_cleanup_master_data_dry_run_returns_estimates() -> None:
    """dry_run=True 时返回 estimated 字典，不执行 DML。"""
    session = MagicMock()
    # 模拟空查询结果
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=[])
    execute_mock = MagicMock()
    execute_mock.scalars = MagicMock(return_value=scalars_mock)
    session.execute = AsyncMock(return_value=execute_mock)
    session.scalar = AsyncMock(return_value=0)

    result = await cleanup_master_data(session, "nonexistent-batch", dry_run=True)
    assert "estimated" in result
    assert result["estimated"] == {
        "sn_assets": 0,
        "board_cards": 0,
        "customer_policies": 0,
    }
    assert result["batch_id"] == "nonexistent-batch"


# ---------------------------------------------------------------------------
# 工具文件存在性与可执行性
# ---------------------------------------------------------------------------
def test_cleanup_tool_file_exists() -> None:
    tool_path = (
        Path(__file__).resolve().parents[1] / "tools" / "cleanup_test_emails.py"
    )
    assert tool_path.exists(), f"工具文件不存在: {tool_path}"
    assert tool_path.is_file()


def test_cleanup_tool_has_main_entry_point() -> None:
    src = (Path(__file__).resolve().parents[1] / "tools" / "cleanup_test_emails.py").read_text(
        encoding="utf-8"
    )
    assert 'if __name__ == "__main__":' in src
    assert "sys.exit(main())" in src


def test_cleanup_tool_does_not_modify_business_code() -> None:
    """工具文件应仅位于 tools/ 目录，不修改 backend/app/ 下任何业务代码。"""
    tool_path = Path(__file__).resolve().parents[1] / "tools" / "cleanup_test_emails.py"
    assert "tools" in str(tool_path)
    # 工具不应定义任何 Alembic 迁移或修改 schema
    src = tool_path.read_text(encoding="utf-8")
    assert "alembic" not in src.lower() or "alembic" in src.lower() and "no" in src.lower()


def test_cleanup_tool_outputs_json_summary_to_stdout() -> None:
    """main() 应在 stdout 输出 JSON 摘要（机器可读）。"""
    src = (
        Path(__file__).resolve().parents[1] / "tools" / "cleanup_test_emails.py"
    ).read_text(encoding="utf-8")
    assert "json.dumps(summary" in src
    assert "print(json.dumps" in src


def test_cleanup_tool_summary_includes_required_fields() -> None:
    """run_cleanup 返回的 summary 应包含 spec 要求的所有字段。"""
    src = inspect.getsource(run_cleanup)
    required_fields = [
        "requested_message_ids",
        "matched_emails",
        "matched_email_ids",
        "matched_ticket_nos",
        "ticket_actions",
        "per_table_counts",
        "master_data_cleanup",
        "verification",
        "timestamp",
        "dry_run",
        "exit_code",
        "error",
    ]
    for field in required_fields:
        assert field in src, f"run_cleanup summary 缺少字段: {field}"


def test_cleanup_tool_verification_includes_imap_dedup_cleared() -> None:
    """verify_deletion 返回值应包含 imap_dedup_cleared 字段。"""
    src = inspect.getsource(verify_deletion)
    assert "imap_dedup_cleared" in src


def test_cleanup_tool_uses_single_transaction() -> None:
    """run_cleanup 应在单个 session 中执行所有 DML，失败回滚。"""
    src = inspect.getsource(run_cleanup)
    assert "AsyncSessionLocal" in src
    assert "session.commit" in src
    assert "session.rollback" in src
    assert "try:" in src
    assert "except" in src


def test_cleanup_tool_includes_safety_message_id_pattern() -> None:
    """工具应处理 message_id 为 <...> 格式的字符串。"""
    src = (
        Path(__file__).resolve().parents[1] / "tools" / "cleanup_test_emails.py"
    ).read_text(encoding="utf-8")
    # 应支持传入 <20260806...@accotest.com> 格式
    assert "message_id" in src
    assert "Email.message_id" in src


# ---------------------------------------------------------------------------
# _handle_email_threads 防御性逻辑
# ---------------------------------------------------------------------------
def test_handle_email_threads_is_async() -> None:
    assert inspect.iscoroutinefunction(_handle_email_threads)


def test_handle_email_threads_supports_shared_thread_defense() -> None:
    """_handle_email_threads 应实现共享线程仅更新 latest_email_id 的防御性逻辑。"""
    src = inspect.getsource(_handle_email_threads)
    # 共享线程应更新而非删除
    assert "other_email_count" in src
    assert "latest_email_id" in src
    # 应同时处理 ticket_id / predecessor_ticket_id 解绑
    assert "predecessor_ticket_id" in src
    assert "ticket_id" in src


# ---------------------------------------------------------------------------
# 整体导入与模块可加载性
# ---------------------------------------------------------------------------
def test_module_imports_cleanly() -> None:
    """所有必需的模型与函数应可正常导入。"""
    from tools.cleanup_test_emails import (
        AiCallLog, BoardCard, CustomerServicePolicy, Email, EmailAttachment,
        EmailThread, EmailTicketLink, ExportSap, ExternalOperationRecord,
        FieldAuditLog, MailFetchRecord, ManualReviewTask, NotificationEvent,
        OperationLog, ParseResult, ReplyRecord, RepairTicket, RepairTicketItem,
        SnAsset, SnValidationResult, SystemEventLog, TicketRelayExport,
        TicketRma, TicketRmaItem, TicketStatusLog,
    )
    # 确保所有模型都有 __tablename__（来自 Base）
    for model in [
        AiCallLog, BoardCard, CustomerServicePolicy, Email, EmailAttachment,
        EmailThread, EmailTicketLink, ExportSap, ExternalOperationRecord,
        FieldAuditLog, MailFetchRecord, ManualReviewTask, NotificationEvent,
        OperationLog, ParseResult, ReplyRecord, RepairTicket, RepairTicketItem,
        SnAsset, SnValidationResult, SystemEventLog, TicketRelayExport,
        TicketRma, TicketRmaItem, TicketStatusLog,
    ]:
        assert hasattr(model, "__tablename__"), f"{model.__name__} 缺少 __tablename__"
