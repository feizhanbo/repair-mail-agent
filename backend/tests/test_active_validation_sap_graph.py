from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.workflows.email_ticket import external, nodes, routers
from app.workflows.email_ticket.human import apply_human_decision, create_human_task, wait_human_review
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_active_validation_sap_graph(*, checkpointer):
    """Migration-slice graph retained only as a test fixture."""
    builder = StateGraph(EmailTicketState, context_schema=EmailTicketRuntime)
    for name, node in {
        "load_ingested_email": nodes.load_ingested_email,
        "normalize_content": nodes.normalize_content,
        "collect_attachment_results": nodes.collect_attachment_results,
        "classify_and_extract": nodes.classify_and_extract,
        "resolve_business_context": nodes.resolve_business_context,
        "plan_deterministic_validation": nodes.plan_deterministic_validation,
        "validate_ticket": external.validate_ticket,
        "submit_sap": external.submit_sap,
        "reconcile_sap": external.reconcile_sap,
        "wait_external_result": external.wait_external_result,
        "poll_sap": external.poll_sap,
        "prepare_rma": external.prepare_rma,
        "send_rma": external.send_rma,
        "finalize_rma_archive": external.finalize_rma_archive,
        "prepare_reply": external.prepare_reply,
        "send_reply": external.send_reply,
        "create_human_task": create_human_task,
        "wait_human_review": wait_human_review,
        "apply_human_decision": apply_human_decision,
        "finish_resumed": nodes.mark_shadow_resumed,
        "finish_terminal": nodes.mark_shadow_terminal,
        "finish_followup": nodes.mark_shadow_followup,
        "finish_completed": external.finish_completed,
        "finish_validation": nodes.mark_shadow_validation,
    }.items():
        builder.add_node(name, node)
    builder.add_edge(START, "load_ingested_email")
    builder.add_conditional_edges("load_ingested_email", routers.route_load_result, {"normalize": "normalize_content", "error": "create_human_task"})
    builder.add_edge("normalize_content", "collect_attachment_results")
    builder.add_conditional_edges("collect_attachment_results", routers.route_attachment_result, {"classify": "classify_and_extract", "human": "create_human_task"})
    builder.add_conditional_edges("classify_and_extract", routers.route_intent, {"auto": "resolve_business_context", "terminal": "finish_terminal", "human": "create_human_task"})
    builder.add_edge("resolve_business_context", "plan_deterministic_validation")
    builder.add_conditional_edges("plan_deterministic_validation", routers.route_parse_quality, {"validate": "validate_ticket", "followup": "prepare_reply", "human": "create_human_task"})
    builder.add_conditional_edges("validate_ticket", routers.route_validation_result, {"sap": "submit_sap", "complete": "prepare_reply", "followup": "prepare_reply", "human": "create_human_task"})
    builder.add_conditional_edges("submit_sap", routers.route_sap_result, {"wait": "wait_external_result", "reconcile": "reconcile_sap", "submit": "submit_sap", "human": "create_human_task"})
    builder.add_conditional_edges("reconcile_sap", routers.route_sap_result, {"wait": "wait_external_result", "reconcile": "wait_external_result", "submit": "submit_sap", "human": "create_human_task"})
    builder.add_edge("wait_external_result", "poll_sap")
    builder.add_conditional_edges("poll_sap", routers.route_sap_poll_result, {"wait": "wait_external_result", "rma": "prepare_rma", "submit": "submit_sap", "human": "create_human_task"})
    builder.add_conditional_edges("prepare_rma", routers.route_rma_prepare_result, {"send": "send_rma", "archive": "finalize_rma_archive", "human": "create_human_task"})
    builder.add_conditional_edges("send_rma", routers.route_rma_send_result, {"archive": "finalize_rma_archive", "human": "create_human_task"})
    builder.add_conditional_edges("finalize_rma_archive", routers.route_rma_archive_result, {"complete": "finish_completed", "human": "create_human_task"})
    builder.add_conditional_edges("prepare_reply", routers.route_reply_result, {"send": "send_reply", "human": "create_human_task"})
    builder.add_conditional_edges("send_reply", routers.route_reply_send_result, {"complete": "finish_validation", "human": "create_human_task"})
    builder.add_edge("create_human_task", "wait_human_review")
    builder.add_edge("wait_human_review", "apply_human_decision")
    builder.add_conditional_edges("apply_human_decision", routers.route_human_result, {"human": "create_human_task", "validate": "validate_ticket", "reparse": "load_ingested_email", "prepare_reply": "prepare_reply", "send_rma": "send_rma", "send_reply": "send_reply", "terminal": "finish_resumed"})
    for finish in ("finish_resumed", "finish_terminal", "finish_followup", "finish_validation", "finish_completed"):
        builder.add_edge(finish, END)
    return builder.compile(checkpointer=checkpointer)


def _snapshot() -> dict:
    return {
        "ticket_id": 10,
        "ticket_status": "parsed",
        "subject": "repair",
        "latest_reply_segment": "SN001 failure",
        "attachments": [],
        "parse_result": {
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.95,
            "missing_fields": {},
            "conflict_fields": {},
        },
    }


async def _run(*, submit_result: dict, reconcile_result: dict | None = None):
    calls: list[tuple[str, int]] = []

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        calls.append(("validate", request["ticket_id"]))
        return {"status": "ready_for_export", "sap_required": True, "export_id": 20}

    async def submit(export_id: int):
        calls.append(("submit", export_id))
        return {**submit_result, "export_id": export_id}

    async def reconcile(export_id: int):
        calls.append(("reconcile", export_id))
        return {**(reconcile_result or {"status": "submit_unknown"}), "export_id": export_id}

    async def create_human(_request):
        return 99

    graph = build_active_validation_sap_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-sap", "route_history": []},
        {"configurable": {"thread_id": "sap-graph-1"}},
        context=EmailTicketRuntime(
            load_email=load_email,
            validate_ticket=validate,
            submit_sap=submit,
            reconcile_sap=reconcile,
            create_human_task=create_human,
        ),
    )
    return result, calls


@pytest.mark.anyio
async def test_sap_acceptance_waits_without_rma_or_smtp() -> None:
    result, calls = await _run(submit_result={"status": "waiting_sap_result"})

    assert calls == [("validate", 10), ("submit", 20)]
    assert result["execution_state"] == "sap_submit_completed"
    assert result["__interrupt__"][0].value["status"] == "waiting_sap_result"


@pytest.mark.anyio
async def test_unknown_submit_reconciles_instead_of_resubmitting() -> None:
    result, calls = await _run(
        submit_result={"status": "submit_unknown"},
        reconcile_result={"status": "waiting_sap_result"},
    )

    assert calls == [("validate", 10), ("submit", 20), ("reconcile", 20)]
    assert result["__interrupt__"][0].value["status"] == "waiting_sap_result"


@pytest.mark.anyio
async def test_confirmed_absent_submission_is_resubmitted_once() -> None:
    calls: list[tuple[str, int]] = []
    submit_count = 0

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        calls.append(("validate", request["ticket_id"]))
        return {"status": "ready_for_export", "sap_required": True, "export_id": 20}

    async def submit(export_id: int):
        nonlocal submit_count
        submit_count += 1
        calls.append(("submit", export_id))
        if submit_count == 1:
            return {"status": "pending", "export_id": export_id}
        return {"status": "waiting_sap_result", "export_id": export_id}

    graph = build_active_validation_sap_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-safe-resubmit", "route_history": []},
        {"configurable": {"thread_id": "safe-resubmit-1"}},
        context=EmailTicketRuntime(load_email=load_email, validate_ticket=validate, submit_sap=submit),
    )

    assert calls == [("validate", 10), ("submit", 20), ("submit", 20)]
    assert result["__interrupt__"][0].value["status"] == "waiting_sap_result"


@pytest.mark.anyio
async def test_repeated_pending_submission_stops_at_human_review() -> None:
    calls: list[tuple[str, int]] = []

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        calls.append(("validate", request["ticket_id"]))
        return {"status": "ready_for_export", "sap_required": True, "export_id": 20}

    async def submit(export_id: int):
        calls.append(("submit", export_id))
        return {"status": "pending", "export_id": export_id}

    async def create_human(_request: dict):
        calls.append(("human", 99))
        return 99

    graph = build_active_validation_sap_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-bounded-resubmit", "route_history": []},
        {"configurable": {"thread_id": "bounded-resubmit-1"}},
        context=EmailTicketRuntime(
            load_email=load_email,
            validate_ticket=validate,
            submit_sap=submit,
            create_human_task=create_human,
        ),
    )

    assert calls == [("validate", 10), ("submit", 20), ("submit", 20), ("human", 99)]
    assert result["sap_submit_attempt_count"] == 2
    assert result["sap_submit_export_id"] == 20
    assert result["__interrupt__"][0].value["task_id"] == 99


@pytest.mark.anyio
async def test_explicit_submit_failure_routes_external_manual_without_retry() -> None:
    result, calls = await _run(submit_result={"status": "submit_failed"})

    assert calls == [("validate", 10), ("submit", 20)]
    assert result["execution_state"] == "waiting_human_review"
    assert result["__interrupt__"][0].value["task_id"] == 99


@pytest.mark.anyio
async def test_poll_pending_resubmits_once_without_legacy_job_or_infinite_loop() -> None:
    """Reconcile-confirmed pending from poll resubmits exactly once in Graph."""
    calls: list[tuple[str, int]] = []
    poll_count = 0

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        calls.append(("validate", request["ticket_id"]))
        return {"status": "ready_for_export", "sap_required": True, "export_id": 20}

    async def submit(export_id: int):
        calls.append(("submit", export_id))
        return {"status": "waiting_sap_result", "export_id": export_id}

    async def poll(export_id: int):
        nonlocal poll_count
        poll_count += 1
        calls.append(("poll", export_id))
        if poll_count == 1:
            # Reconcile confirmed the remote rows are absent: the documented
            # single safe resubmission must run inside the Graph, not a legacy
            # relay_ticket_export job, and must never loop back into poll.
            return {"status": "pending", "export_id": export_id}
        return {"status": "rma_received", "export_id": export_id, "rma_no": "RMA001"}

    async def prepare_rma(request: dict):
        calls.append(("prepare_rma", request["ticket_id"]))
        return {"status": "prepared", "ticket_id": 10, "reply_id": 30}

    async def send_rma(reply_id: int):
        calls.append(("send_rma", reply_id))
        return {"status": "sent", "ticket_id": 10, "reply_id": reply_id}

    async def archive(reply_id: int):
        calls.append(("archive", reply_id))
        return {"status": "closed", "ticket_id": 10, "reply_id": reply_id}

    config = {"configurable": {"thread_id": "poll-pending-resubmit-1"}}
    checkpointer = InMemorySaver()
    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    context = EmailTicketRuntime(
        load_email=load_email,
        validate_ticket=validate,
        submit_sap=submit,
        poll_sap=poll,
        prepare_rma=prepare_rma,
        send_rma=send_rma,
        finalize_rma_archive=archive,
    )

    first = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-poll-pending", "route_history": []},
        config,
        context=context,
    )
    assert first["__interrupt__"][0].value["status"] == "waiting_sap_result"
    assert calls == [("validate", 10), ("submit", 20)]

    # First scheduled poll confirms the remote rows are absent -> pending.
    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    second = await graph.ainvoke(Command(resume={"reason": "scheduled_poll"}), config, context=context)
    assert second["__interrupt__"][0].value["status"] == "waiting_sap_result"
    assert calls == [("validate", 10), ("submit", 20), ("poll", 20), ("submit", 20)]
    assert poll_count == 1

    # The next resume must not resubmit again; poll sees the RMA and finishes.
    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    completed = await graph.ainvoke(Command(resume={"reason": "scheduled_poll"}), config, context=context)
    assert completed["execution_state"] == "completed"
    assert calls == [
        ("validate", 10),
        ("submit", 20),
        ("poll", 20),
        ("submit", 20),
        ("poll", 20),
        ("prepare_rma", 10),
        ("send_rma", 30),
        ("archive", 30),
    ]
    assert poll_count == 2


@pytest.mark.anyio
async def test_ready_ticket_without_sap_requirement_never_calls_submit() -> None:
    calls: list[str] = []

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        assert request["ticket_id"] == 10
        return {"status": "ready_for_export", "sap_required": False, "export_id": None}

    async def submit(_export_id: int):
        calls.append("submit")
        raise AssertionError("SAP must not run when not required")

    async def prepare_reply(request: dict):
        assert request["ticket_id"] == 10
        return {
            "status": "prepared",
            "reply_id": 40,
            "send_status": "approved_pending_send",
        }

    async def send_reply(reply_id: int):
        assert reply_id == 40
        return {"status": "sent", "reply_id": reply_id}

    async def apply_human(request: dict):
        assert request["action"] == "approve_send"
        calls.append("apply_human")
        return {"status": "applied", "action": "approve_send"}

    graph = build_active_validation_sap_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-no-sap", "route_history": []},
        {"configurable": {"thread_id": "no-sap-1"}},
        context=EmailTicketRuntime(
            load_email=load_email,
            validate_ticket=validate,
            submit_sap=submit,
            prepare_reply=prepare_reply,
            send_reply=send_reply,
        ),
    )

    assert calls == []
    assert result["shadow_outcome"] == "deterministic_validation_required"


@pytest.mark.anyio
async def test_resume_completes_split_rma_side_effects_without_resubmitting_sap() -> None:
    calls: list[tuple[str, int]] = []
    poll_count = 0

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        calls.append(("validate", request["ticket_id"]))
        return {"status": "ready_for_export", "sap_required": True, "export_id": 20}

    async def submit(export_id: int):
        calls.append(("submit", export_id))
        return {"status": "waiting_sap_result", "export_id": export_id}

    async def poll(export_id: int):
        nonlocal poll_count
        poll_count += 1
        calls.append(("poll", export_id))
        if poll_count == 1:
            return {"status": "waiting_rma", "export_id": export_id, "next_poll_seconds": 60}
        return {"status": "rma_received", "export_id": export_id, "rma_no": "RMA001"}

    async def prepare(request: dict):
        assert request == {"ticket_id": 10, "rma_no": "RMA001"}
        calls.append(("prepare", 10))
        return {"status": "prepared", "ticket_id": 10, "reply_id": 30}

    async def send(reply_id: int):
        calls.append(("send", reply_id))
        return {"status": "sent", "ticket_id": 10, "reply_id": reply_id}

    async def archive(reply_id: int):
        calls.append(("archive", reply_id))
        return {"status": "closed", "ticket_id": 10, "reply_id": reply_id}

    config = {"configurable": {"thread_id": "sap-resume-1"}}
    checkpointer = InMemorySaver()
    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    context = EmailTicketRuntime(
        load_email=load_email,
        validate_ticket=validate,
        submit_sap=submit,
        poll_sap=poll,
        prepare_rma=prepare,
        send_rma=send,
        finalize_rma_archive=archive,
    )

    first = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-resume", "route_history": []},
        config,
        context=context,
    )
    assert first["__interrupt__"][0].value["status"] == "waiting_sap_result"

    # Recompile against the same durable store to simulate process restart.
    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    second = await graph.ainvoke(Command(resume={"reason": "scheduled_poll"}), config, context=context)
    assert second["__interrupt__"][0].value["status"] == "waiting_rma"

    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    completed = await graph.ainvoke(Command(resume={"reason": "scheduled_poll"}), config, context=context)
    assert completed["execution_state"] == "completed"
    assert completed["shadow_outcome"] == "completed"
    assert calls == [
        ("validate", 10),
        ("submit", 20),
        ("poll", 20),
        ("poll", 20),
        ("prepare", 10),
        ("send", 30),
        ("archive", 30),
    ]


@pytest.mark.anyio
async def test_reply_review_resume_sends_prepared_reply_once() -> None:
    calls: list[str] = []

    async def load_email(_email_id: int):
        return _snapshot()

    async def validate(request: dict):
        assert request["ticket_id"] == 10
        return {"status": "ready_for_export", "sap_required": False}

    async def prepare_reply(_request: dict):
        calls.append("prepare")
        return {
            "status": "prepared",
            "reply_id": 41,
            "send_status": "pending_review",
        }

    async def create_human(request: dict):
        assert request["reply_id"] == 41
        calls.append("human")
        return 99

    async def send_reply(reply_id: int):
        assert reply_id == 41
        calls.append("send")
        return {"status": "sent", "reply_id": reply_id}

    async def apply_human(request: dict):
        assert request["action"] == "approve_send"
        calls.append("apply_human")
        return {"status": "applied", "action": "approve_send"}

    config = {"configurable": {"thread_id": "reply-review-1"}}
    checkpointer = InMemorySaver()
    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    context = EmailTicketRuntime(
        load_email=load_email,
        validate_ticket=validate,
        prepare_reply=prepare_reply,
        create_human_task=create_human,
        send_reply=send_reply,
        apply_human_decision=apply_human,
    )
    interrupted = await graph.ainvoke(
        {"email_id": 1, "execution_id": "exec-reply-review", "route_history": []},
        config,
        context=context,
    )
    assert interrupted["__interrupt__"][0].value["reply_id"] == 41

    graph = build_active_validation_sap_graph(checkpointer=checkpointer)
    completed = await graph.ainvoke(
        Command(resume={"task_id": 99, "action": "approve_send"}),
        config,
        context=context,
    )
    assert completed["shadow_outcome"] == "deterministic_validation_required"
    assert calls == ["prepare", "human", "apply_human", "send"]
