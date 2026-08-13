from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.services import emails
from app.workflows.email_ticket.active_graph import build_active_email_ticket_graph
from app.workflows.email_ticket.state import EmailTicketRuntime


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_only_email_dispatch_service_can_enqueue_graph_start() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    owners: list[str] = []
    for module_path in app_root.rglob("*.py"):
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if function_name != "enqueue_job":
                continue
            job_type = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "job_type"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            if job_type == "graph_start":
                owners.append(module_path.relative_to(app_root).as_posix())

    assert owners == ["services/emails.py"]


@pytest.mark.anyio
async def test_authoritative_graph_owns_parse_adopt_validate_and_reply() -> None:
    calls: list[str] = []

    async def prepare(request: dict):
        calls.append("prepare")
        assert request["email_id"] == 1
        return {"email_id": 1, "rule_parse_result_id": 2, "attachment_ids": []}

    async def ai_candidate(context: dict):
        calls.append("ai")
        assert context["rule_parse_result_id"] == 2
        return {"ai_available": True, "ai_parse_result_id": 3}

    async def adopt(request: dict):
        calls.append("adopt")
        assert request["ai_candidate"]["ai_parse_result_id"] == 3
        return {
            "email_parse_status": "parsed",
            "ticket_id": 10,
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.98,
            "missing_fields": {},
            "conflict_fields": {},
        }

    async def validate(request: dict):
        calls.append("validate")
        assert request["ticket_id"] == 10
        assert request["resolving_task_id"] is None
        return {"status": "ready_for_export", "sap_required": False}

    async def prepare_reply(request: dict):
        calls.append("prepare_reply")
        return {
            "status": "prepared",
            "reply_id": 20,
            "send_status": "approved_pending_send",
        }

    async def send_reply(reply_id: int):
        calls.append("send_reply")
        assert reply_id == 20
        return {"status": "sent", "reply_id": reply_id}

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "authoritative-1", "route_history": []},
        {"configurable": {"thread_id": "authoritative-1"}},
        context=EmailTicketRuntime(
            load_email=AsyncMock(),
            prepare_email_parse=prepare,
            generate_ai_candidate=ai_candidate,
            adopt_email_candidate=adopt,
            validate_ticket=validate,
            prepare_reply=prepare_reply,
            send_reply=send_reply,
        ),
    )

    assert calls == ["prepare", "ai", "adopt", "validate", "prepare_reply", "send_reply"]
    assert result["workflow_outcome"] == "reply_sent"
    assert result["execution_state"] == "completed"


@pytest.mark.anyio
async def test_authoritative_graph_restarts_through_sap_rma_without_replaying_parse() -> None:
    calls: list[str] = []
    poll_count = 0

    async def prepare(_request: dict):
        calls.append("prepare")
        return {"email_id": 1, "rule_parse_result_id": 2, "attachment_ids": []}

    async def ai(_context: dict):
        calls.append("ai")
        return {"ai_available": True, "ai_parse_result_id": 3}

    async def adopt(_request: dict):
        calls.append("adopt")
        return {
            "email_parse_status": "parsed",
            "ticket_id": 10,
            "ticket_version": 2,
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.98,
            "missing_fields": {},
            "conflict_fields": {},
        }

    async def validate(request: dict):
        calls.append("validate")
        assert request["ticket_id"] == 10
        assert request["resolving_task_id"] is None
        return {"status": "ready_for_export", "sap_required": True, "export_id": 20}

    async def submit(_export_id: int):
        calls.append("submit")
        return {"status": "waiting_sap_result", "export_id": 20, "next_poll_seconds": 60}

    async def poll(_export_id: int):
        nonlocal poll_count
        poll_count += 1
        calls.append(f"poll:{poll_count}")
        if poll_count == 1:
            return {"status": "waiting_rma", "export_id": 20, "next_poll_seconds": 60}
        return {"status": "rma_received", "export_id": 20, "rma_no": "RMA001"}

    async def prepare_rma(request: dict):
        calls.append("prepare_rma")
        assert request == {"ticket_id": 10, "rma_no": "RMA001"}
        return {"status": "prepared", "ticket_id": 10, "reply_id": 30}

    async def send_rma(_reply_id: int):
        calls.append("send_rma")
        return {"status": "sent", "ticket_id": 10, "reply_id": 30}

    async def archive(_reply_id: int):
        calls.append("archive")
        return {"status": "closed", "ticket_id": 10, "reply_id": 30}

    context = EmailTicketRuntime(
        load_email=AsyncMock(),
        prepare_email_parse=prepare,
        generate_ai_candidate=ai,
        adopt_email_candidate=adopt,
        validate_ticket=validate,
        submit_sap=submit,
        poll_sap=poll,
        prepare_rma=prepare_rma,
        send_rma=send_rma,
        finalize_rma_archive=archive,
    )
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "authoritative-sap-restart"}}
    graph = build_active_email_ticket_graph(checkpointer=saver)

    first = await graph.ainvoke(
        {"email_id": 1, "execution_id": "authoritative-sap", "route_history": []},
        config,
        context=context,
    )
    assert first["__interrupt__"][0].value["status"] == "waiting_sap_result"

    graph = build_active_email_ticket_graph(checkpointer=saver)
    second = await graph.ainvoke(Command(resume={"reason": "scheduled_poll"}), config, context=context)
    assert second["__interrupt__"][0].value["status"] == "waiting_rma"

    graph = build_active_email_ticket_graph(checkpointer=saver)
    completed = await graph.ainvoke(Command(resume={"reason": "scheduled_poll"}), config, context=context)

    assert completed["workflow_outcome"] == "completed"
    assert calls == [
        "prepare", "ai", "adopt", "validate", "submit",
        "poll:1", "poll:2", "prepare_rma", "send_rma", "archive",
    ]


@pytest.mark.anyio
async def test_authoritative_human_resume_after_restart_continues_without_legacy_replay() -> None:
    calls: list[str] = []

    async def prepare(_request: dict):
        calls.append("prepare")
        return {"email_id": 1, "rule_parse_result_id": 2, "attachment_ids": []}

    async def ai(_context: dict):
        calls.append("ai")
        return {"ai_available": True, "ai_parse_result_id": 3}

    async def adopt(_request: dict):
        calls.append("adopt")
        return {
            "email_parse_status": "needs_manual",
            "ticket_id": 10,
            "ticket_version": 4,
            "manual_task_id": 91,
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.4,
            "missing_fields": {},
            "conflict_fields": {"customer_code": "conflict"},
        }

    async def apply_human(request: dict):
        calls.append("apply_human")
        assert request["task_id"] == 91
        assert request["expected_ticket_version"] == 4
        return {"status": "applied", "action": "validate"}

    async def validate(request: dict):
        calls.append("validate")
        assert request["ticket_id"] == 10
        assert request["resolving_task_id"] == 91
        return {"status": "ready_for_export", "sap_required": False}

    async def prepare_reply(_request: dict):
        calls.append("prepare_reply")
        return {
            "status": "prepared",
            "reply_id": 41,
            "send_status": "approved_pending_send",
        }

    async def send_reply(_reply_id: int):
        calls.append("send_reply")
        return {"status": "sent", "reply_id": 41}

    context = EmailTicketRuntime(
        load_email=AsyncMock(),
        prepare_email_parse=prepare,
        generate_ai_candidate=ai,
        adopt_email_candidate=adopt,
        apply_human_decision=apply_human,
        validate_ticket=validate,
        prepare_reply=prepare_reply,
        send_reply=send_reply,
    )
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "authoritative-human-restart"}}
    graph = build_active_email_ticket_graph(checkpointer=saver)

    interrupted = await graph.ainvoke(
        {"email_id": 1, "execution_id": "authoritative-human", "route_history": []},
        config,
        context=context,
    )
    request = interrupted["__interrupt__"][0].value
    assert request["task_id"] == 91
    assert request["expected_ticket_version"] == 4

    graph = build_active_email_ticket_graph(checkpointer=saver)
    completed = await graph.ainvoke(
        Command(
            resume={
                "task_id": 91,
                "action": "validate",
                "expected_ticket_version": 4,
                "reviewer_id": 7,
            }
        ),
        config,
        context=context,
    )

    assert completed["workflow_outcome"] == "reply_sent"
    assert calls == [
        "prepare", "ai", "adopt", "apply_human", "validate", "prepare_reply", "send_reply",
    ]


@pytest.mark.anyio
async def test_langgraph_dispatcher_never_calls_legacy_reparse(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 100)
    legacy = AsyncMock(side_effect=AssertionError("legacy parser must not run"))
    monkeypatch.setattr(emails, "reparse_email", legacy)
    queued = SimpleNamespace(id=7, status="queued")
    enqueue = AsyncMock(return_value=queued)
    monkeypatch.setattr(emails, "enqueue_job", enqueue)
    monkeypatch.setattr(emails, "_find_active_email_graph_dispatch", AsyncMock(return_value=(None, None)))

    result = await emails.dispatch_email_parse(
        SimpleNamespace(),
        email_id=11,
        user_id=5,
        rule_parse_result_id=13,
    )

    legacy.assert_not_awaited()
    assert result["workflow"]["execution_id"] == "email-11-rule-13"
    assert enqueue.await_args.kwargs["job_type"] == "graph_start"


@pytest.mark.anyio
async def test_legacy_dispatcher_keeps_existing_orchestrator(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "legacy")
    legacy = AsyncMock(return_value={"parse_result": {"id": 3}})
    monkeypatch.setattr(emails, "reparse_email", legacy)

    result = await emails.dispatch_email_parse(SimpleNamespace(), email_id=11, user_id=5)

    assert result == {"parse_result": {"id": 3}}
    legacy.assert_awaited_once()


@pytest.mark.anyio
async def test_explicit_reparse_creates_a_new_execution_each_time(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 100)
    enqueue = AsyncMock(return_value=SimpleNamespace(id=9, status="queued"))
    monkeypatch.setattr(emails, "enqueue_job", enqueue)
    monkeypatch.setattr(emails, "_find_active_email_graph_dispatch", AsyncMock(return_value=(None, None)))

    first = await emails.dispatch_email_parse(SimpleNamespace(), email_id=11)
    second = await emails.dispatch_email_parse(SimpleNamespace(), email_id=11)

    assert first["workflow"]["execution_id"] != second["workflow"]["execution_id"]


@pytest.mark.anyio
async def test_background_reparse_can_supply_stable_retry_execution_id(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 100)
    enqueue = AsyncMock(return_value=SimpleNamespace(id=10, status="queued"))
    monkeypatch.setattr(emails, "enqueue_job", enqueue)
    monkeypatch.setattr(emails, "_find_active_email_graph_dispatch", AsyncMock(return_value=(None, None)))

    first = await emails.dispatch_email_parse(
        SimpleNamespace(), email_id=11, workflow_execution_id="email-11-job-7"
    )
    second = await emails.dispatch_email_parse(
        SimpleNamespace(), email_id=11, workflow_execution_id="email-11-job-7"
    )

    assert first["workflow"]["execution_id"] == second["workflow"]["execution_id"] == "email-11-job-7"
    assert enqueue.await_args_list[0].kwargs["idempotency_key"] == "graph_start:email-11-job-7"


@pytest.mark.anyio
async def test_langgraph_rollout_uses_stable_email_bucket_and_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 10)
    monkeypatch.setattr(emails.settings, "LANGGRAPH_EMAIL_ALLOWLIST", [42])
    legacy = AsyncMock(return_value={"engine": "legacy"})
    monkeypatch.setattr(emails, "reparse_email", legacy)
    enqueue = AsyncMock(return_value=SimpleNamespace(id=8, status="queued"))
    monkeypatch.setattr(emails, "enqueue_job", enqueue)
    monkeypatch.setattr(emails, "_find_active_email_graph_dispatch", AsyncMock(return_value=(None, None)))

    monkeypatch.setattr(emails, "sha256_text", lambda value: "00000005" + "0" * 56 if value == "email:5" else "0000000f" + "0" * 56)
    assert (await emails.dispatch_email_parse(SimpleNamespace(), email_id=5))["workflow"]["engine"] == "langgraph"
    assert (await emails.dispatch_email_parse(SimpleNamespace(), email_id=42))["workflow"]["engine"] == "langgraph"
    assert await emails.dispatch_email_parse(SimpleNamespace(), email_id=15) == {"engine": "legacy"}
    assert enqueue.await_count == 2
    legacy.assert_awaited_once()


@pytest.mark.anyio
async def test_dispatch_reuses_active_email_workflow_execution(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 100)
    execution = SimpleNamespace(
        execution_id="email-11-existing",
        trigger_job_id=17,
        status="waiting_human",
    )
    monkeypatch.setattr(
        emails,
        "_find_active_email_graph_dispatch",
        AsyncMock(return_value=(execution, None)),
    )
    enqueue = AsyncMock(side_effect=AssertionError("a second graph must not be queued"))
    monkeypatch.setattr(emails, "enqueue_job", enqueue)

    result = await emails.dispatch_email_parse(SimpleNamespace(), email_id=11)

    assert result == {
        "workflow": {
            "engine": "langgraph",
            "execution_id": "email-11-existing",
            "job_id": 17,
            "status": "waiting_human",
        },
        "status": "workflow_active",
        "email_id": 11,
    }
    enqueue.assert_not_awaited()


@pytest.mark.anyio
async def test_failed_email_workflow_remains_owner_until_explicit_recovery(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 100)
    execution = SimpleNamespace(
        execution_id="email-11-failed",
        trigger_job_id=23,
        status="failed",
    )
    monkeypatch.setattr(
        emails,
        "_find_active_email_graph_dispatch",
        AsyncMock(return_value=(execution, None)),
    )
    enqueue = AsyncMock(side_effect=AssertionError("failed graph must be recovered, not replaced"))
    monkeypatch.setattr(emails, "enqueue_job", enqueue)

    result = await emails.dispatch_email_parse(SimpleNamespace(), email_id=11)

    assert result["workflow"]["execution_id"] == "email-11-failed"
    assert result["workflow"]["status"] == "failed"
    assert result["status"] == "workflow_active"
    enqueue.assert_not_awaited()


@pytest.mark.anyio
async def test_dispatch_reuses_graph_start_before_execution_row_exists(monkeypatch) -> None:
    monkeypatch.setattr(emails.settings, "WORKFLOW_ENGINE", "langgraph")
    monkeypatch.setattr(emails.settings, "LANGGRAPH_ROLLOUT_PERCENT", 100)
    job = SimpleNamespace(
        id=19,
        status="queued",
        metadata_json={"execution_id": "email-11-reparse-existing"},
    )
    monkeypatch.setattr(
        emails,
        "_find_active_email_graph_dispatch",
        AsyncMock(return_value=(None, job)),
    )
    enqueue = AsyncMock(side_effect=AssertionError("a second graph must not be queued"))
    monkeypatch.setattr(emails, "enqueue_job", enqueue)

    result = await emails.dispatch_email_parse(SimpleNamespace(), email_id=11)

    assert result["workflow"] == {
        "engine": "langgraph",
        "execution_id": "email-11-reparse-existing",
        "job_id": 19,
        "status": "queued",
    }
    assert result["status"] == "workflow_active"
    enqueue.assert_not_awaited()


@pytest.mark.anyio
async def test_active_email_graph_lookup_locks_email_before_checking_owners() -> None:
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(id=11)),
        scalar=AsyncMock(side_effect=[None, None]),
    )

    assert await emails._find_active_email_graph_dispatch(session, email_id=11) == (None, None)

    session.get.assert_awaited_once_with(
        emails.Email,
        11,
        with_for_update=True,
        populate_existing=True,
    )
    assert session.scalar.await_count == 2


@pytest.mark.anyio
async def test_active_email_graph_lookup_rejects_missing_email() -> None:
    session = SimpleNamespace(get=AsyncMock(return_value=None), scalar=AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await emails._find_active_email_graph_dispatch(session, email_id=404)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "EMAIL_NOT_FOUND"
    session.scalar.assert_not_awaited()


@pytest.mark.anyio
async def test_missing_information_prepares_followup_without_validation() -> None:
    calls: list[str] = []

    async def prepare(_request: dict):
        return {"email_id": 1, "rule_parse_result_id": 2, "attachment_ids": []}

    async def ai(_context: dict):
        return {"ai_available": True, "ai_parse_result_id": 3}

    async def adopt(_request: dict):
        return {
            "email_parse_status": "parsed",
            "ticket_id": 10,
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.95,
            "missing_fields": {"contact_phone": "required"},
            "conflict_fields": {},
        }

    async def forbidden_validate(request: dict):
        del request
        raise AssertionError("missing information must not enter export validation")

    async def prepare_reply(request: dict):
        calls.append(request["reply_type"])
        return {
            "status": "prepared",
            "reply_id": 20,
            "send_status": "approved_pending_send",
        }

    async def send(reply_id: int):
        calls.append(f"send:{reply_id}")
        return {"status": "sent", "reply_id": reply_id}

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "missing-1", "route_history": []},
        {"configurable": {"thread_id": "missing-1"}},
        context=EmailTicketRuntime(
            load_email=AsyncMock(),
            prepare_email_parse=prepare,
            generate_ai_candidate=ai,
            adopt_email_candidate=adopt,
            validate_ticket=forbidden_validate,
            prepare_reply=prepare_reply,
            send_reply=send,
        ),
    )

    assert calls == ["missing_fields", "send:20"]
    assert result["workflow_outcome"] == "customer_followup_sent"
    assert result["execution_state"] == "completed"


@pytest.mark.anyio
async def test_adopted_irrelevant_email_terminates_without_ticket_services() -> None:
    async def prepare(_request: dict):
        return {"email_id": 1, "rule_parse_result_id": 2, "attachment_ids": []}

    async def ai(_context: dict):
        return {"ai_available": True, "ai_parse_result_id": 3}

    async def adopt(_request: dict):
        return {
            "email_parse_status": "skipped",
            "ticket_id": None,
            "intent_type": "irrelevant",
            "missing_fields": {},
            "conflict_fields": {},
        }

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "irrelevant-1", "route_history": []},
        {"configurable": {"thread_id": "irrelevant-1"}},
        context=EmailTicketRuntime(
            load_email=AsyncMock(),
            prepare_email_parse=prepare,
            generate_ai_candidate=ai,
            adopt_email_candidate=adopt,
        ),
    )

    assert result["workflow_outcome"] == "terminal_without_ticket_automation"
    assert result["execution_state"] == "completed"


@pytest.mark.anyio
async def test_recoverable_parse_error_enters_common_human_interrupt() -> None:
    async def prepare(_request: dict):
        raise HTTPException(status_code=409, detail="EMAIL_PARSE_CONFLICT")

    async def create_task(request: dict) -> int:
        assert request["reasons"] == ["prepare_email_parse:EMAIL_PARSE_CONFLICT"]
        return 91

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "recoverable-1", "route_history": []},
        {"configurable": {"thread_id": "recoverable-1"}},
        context=EmailTicketRuntime(
            load_email=AsyncMock(),
            prepare_email_parse=prepare,
            create_human_task=create_task,
        ),
    )

    assert result["error"] == {
        "code": "EMAIL_PARSE_CONFLICT",
        "stage": "prepare_email_parse",
        "retryable": False,
        "recoverable": True,
    }
    assert result["manual_task_id"] == 91
    assert result["execution_state"] == "waiting_human_review"
    assert result["__interrupt__"]


@pytest.mark.anyio
async def test_system_failure_escapes_graph_for_job_retry() -> None:
    async def prepare(_request: dict):
        raise RuntimeError("DATABASE_UNAVAILABLE")

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    with pytest.raises(RuntimeError, match="DATABASE_UNAVAILABLE"):
        await graph.ainvoke(
            {"email_id": 1, "execution_id": "retryable-1", "route_history": []},
            {"configurable": {"thread_id": "retryable-1"}},
            context=EmailTicketRuntime(
                load_email=AsyncMock(),
                prepare_email_parse=prepare,
            ),
        )


@pytest.mark.anyio
async def test_rate_limit_escapes_graph_for_job_retry() -> None:
    async def prepare(_request: dict):
        raise HTTPException(status_code=429, detail="RATE_LIMITED")

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    with pytest.raises(HTTPException) as captured:
        await graph.ainvoke(
            {"email_id": 1, "execution_id": "rate-limit-1", "route_history": []},
            {"configurable": {"thread_id": "rate-limit-1"}},
            context=EmailTicketRuntime(
                load_email=AsyncMock(),
                prepare_email_parse=prepare,
            ),
        )
    assert captured.value.status_code == 429


@pytest.mark.anyio
async def test_recoverable_error_state_does_not_store_arbitrary_exception_text() -> None:
    async def prepare(_request: dict):
        raise ValueError("invalid value supplied by alice@example.com")

    async def create_task(request: dict) -> int:
        assert request["reasons"] == ["prepare_email_parse:WORKFLOW_BUSINESS_ERROR"]
        return 92

    graph = build_active_email_ticket_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "sanitized-error-1", "route_history": []},
        {"configurable": {"thread_id": "sanitized-error-1"}},
        context=EmailTicketRuntime(
            load_email=AsyncMock(),
            prepare_email_parse=prepare,
            create_human_task=create_task,
        ),
    )

    assert result["error"]["code"] == "WORKFLOW_BUSINESS_ERROR"
    assert "alice" not in str(result)
