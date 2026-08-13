from __future__ import annotations

import pytest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.workflows.email_ticket.human import allowed_human_actions, wait_human_review
from app.workflows.email_ticket.graph import build_shadow_email_ticket_graph
from app.workflows.email_ticket.state import EmailTicketRuntime, EmailTicketState


def _graph():
    builder = StateGraph(EmailTicketState)
    builder.add_node("wait_human_review", wait_human_review)
    builder.add_edge(START, "wait_human_review")
    builder.add_edge("wait_human_review", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"email_id": 1}, ["reparse", "close"]),
        (
            {"email_id": 1, "ticket_id": 2},
            ["reparse", "validate", "request_customer_info", "close"],
        ),
        (
            {"email_id": 1, "ticket_id": 2, "reply_result": {"reply_id": 3}},
            [
                "reparse",
                "validate",
                "request_customer_info",
                "approve_send",
                "reject_send",
                "close",
            ],
        ),
        (
            {"email_id": 1, "ticket_id": 2, "rma_result": {"reply_id": 4}},
            [
                "reparse",
                "validate",
                "request_customer_info",
                "approve_send",
                "reject_send",
                "close",
            ],
        ),
    ],
)
def test_allowed_human_actions_are_derived_from_workflow_facts(state, expected) -> None:
    assert allowed_human_actions(state) == expected


def test_human_interrupt_can_resume_with_same_thread_without_side_effect_replay() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "human-review-1"}}
    first = graph.invoke(
        {
            "execution_id": "exec-1",
            "email_id": 11,
                "ticket_id": 22,
                "manual_task_id": 33,
            "validation_plan": {"reasons": ["CUSTOMER_CONFLICT"]},
            "route_history": [],
        },
        config,
    )

    assert first["__interrupt__"][0].value["schema_version"] == "human-review-v1"
    resumed = graph.invoke(
        Command(
            resume={
                "task_id": 33,
                "action": "validate",
                "edited_fields": {"customer_code": "C001"},
                "reviewer_id": 7,
                "expected_ticket_version": 4,
            }
        ),
        config,
    )

    assert resumed["execution_state"] == "human_review_completed"
    assert resumed["human_result"]["action"] == "validate"
    assert resumed["human_result"]["expected_ticket_version"] == 4


def test_new_thread_does_not_consume_another_threads_resume_value() -> None:
    graph = _graph()
    initial = {"execution_id": "exec-2", "email_id": 12, "route_history": []}

    first = graph.invoke(initial, {"configurable": {"thread_id": "human-review-a"}})
    second = graph.invoke(initial, {"configurable": {"thread_id": "human-review-b"}})

    assert "__interrupt__" in first
    assert "__interrupt__" in second


def test_human_resume_rejects_stale_ticket_version() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "human-review-stale"}}
    graph.invoke(
        {
            "execution_id": "exec-stale",
            "email_id": 11,
            "ticket_id": 22,
            "ticket_version_snapshot": 3,
            "manual_task_id": 33,
            "route_history": [],
        },
        config,
    )

    with pytest.raises(ValueError, match="HUMAN_TICKET_VERSION_MISMATCH"):
        graph.invoke(
            Command(
                resume={
                    "task_id": 33,
                    "action": "validate",
                    "expected_ticket_version": 4,
                }
            ),
            config,
        )


def test_human_resume_rejects_action_not_advertised_by_interrupt() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "human-review-action"}}
    interrupted = graph.invoke(
        {
            "execution_id": "exec-action",
            "email_id": 11,
            "manual_task_id": 33,
            "route_history": [],
        },
        config,
    )
    assert interrupted["__interrupt__"][0].value["allowed_actions"] == ["reparse", "close"]

    with pytest.raises(ValueError, match="HUMAN_ACTION_NOT_ALLOWED"):
        graph.invoke(
            Command(resume={"task_id": 33, "action": "approve_send"}),
            config,
        )


@pytest.mark.anyio
async def test_email_graph_interrupts_and_resumes_to_requested_action() -> None:
    async def load_email(_email_id: int):
        return {
            "subject": "repair",
            "latest_reply_segment": "uncertain customer",
            "attachments": [],
            "parse_result": {
                "intent_type": "new_repair",
                "handling_level": "auto_repair",
                "confidence_score": 0.2,
                "missing_fields": {},
                "conflict_fields": {},
            },
        }

    created_tasks: list[dict] = []

    async def create_task(request: dict) -> int:
        created_tasks.append(request)
        return 1

    graph = build_shadow_email_ticket_graph(
        checkpointer=InMemorySaver(),
        interrupt_on_human=True,
    )
    config = {"configurable": {"thread_id": "email-human-1"}}
    first = await graph.ainvoke(
        {"email_id": 11, "execution_id": "exec-email", "route_history": []},
        config,
        context=EmailTicketRuntime(load_email=load_email, create_human_task=create_task),
    )
    assert first["__interrupt__"][0].value["email_id"] == 11
    assert first["__interrupt__"][0].value["task_id"] == 1
    assert len(created_tasks) == 1

    resumed = await graph.ainvoke(
        Command(resume={"task_id": 1, "action": "reparse"}),
        config,
        context=EmailTicketRuntime(load_email=load_email, create_human_task=create_task),
    )
    assert resumed["shadow_outcome"] == "human_reparse_requested"
    assert len(created_tasks) == 1
