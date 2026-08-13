from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langgraph.runtime import Runtime

from app.workflows.email_ticket import external, nodes
from app.workflows.email_ticket.human import apply_human_decision, create_human_task
from app.workflows.email_ticket.state import EmailTicketRuntime


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _runtime(**callbacks) -> Runtime:
    return Runtime(context=EmailTicketRuntime(load_email=callbacks.pop("load_email", AsyncMock()), **callbacks))


@pytest.mark.anyio
async def test_load_normalize_attachment_and_classification_nodes() -> None:
    snapshot = {
        "thread_id": 2,
        "ticket_id": 3,
        "ticket_status": "parsed",
        "ticket_version": 4,
        "subject": " repair ",
        "latest_reply_segment": " latest ",
        "clean_body": " clean ",
        "attachments": [{"id": 5, "parse_status": "parsed"}, "ignored"],
        "parse_result": {"intent_type": "new_repair", "handling_level": "auto_repair"},
    }
    loaded = await nodes.load_ingested_email(
        {"email_id": 1}, _runtime(load_email=AsyncMock(return_value=snapshot))
    )
    assert loaded["ticket_id"] == 3
    assert loaded["ticket_version_snapshot"] == 4

    normalized = nodes.normalize_content({"email_snapshot": snapshot})
    assert normalized["normalized_content"] == {
        "subject": " repair ",
        "latest_reply": "latest",
        "clean_body": "clean",
    }
    attachments = nodes.collect_attachment_results({"email_snapshot": snapshot})
    assert attachments["attachment_results"] == [{"id": 5, "parse_status": "parsed"}]

    classified = await nodes.classify_and_extract(
        {
            "email_id": 1,
            "email_snapshot": snapshot,
            "normalized_content": normalized["normalized_content"],
            "attachment_results": attachments["attachment_results"],
        },
        _runtime(),
    )
    assert classified["ai_result"]["intent_type"] == "new_repair"


@pytest.mark.anyio
async def test_classifier_node_uses_injected_ai_capability() -> None:
    classify = AsyncMock(return_value={"intent_type": "unknown", "handling_level": "unknown"})
    result = await nodes.classify_and_extract(
        {
            "email_id": 8,
            "email_snapshot": {},
            "normalized_content": {"latest_reply": "ambiguous"},
            "attachment_results": [],
        },
        _runtime(classify_email=classify),
    )
    classify.assert_awaited_once_with(
        {"email_id": 8, "content": {"latest_reply": "ambiguous"}, "attachments": []}
    )
    assert result["ai_result"]["intent_type"] == "unknown"


def test_business_context_and_validation_plan_nodes_are_deterministic() -> None:
    context = nodes.resolve_business_context(
        {
            "email_snapshot": {"in_reply_to": "<parent>"},
            "ticket_id": 3,
            "ticket_status_snapshot": "closed",
        }
    )
    assert context["business_context"] == {
        "has_reply_headers": True,
        "has_active_ticket": False,
        "closed_ticket_context": True,
    }

    conflict = nodes.plan_deterministic_validation(
        {"ai_result": {"confidence_score": 1, "conflict_fields": {"sn": ["A", "B"]}}},
        _runtime(),
    )
    missing = nodes.plan_deterministic_validation(
        {"ai_result": {"confidence_score": 1, "missing_fields": {"sn": True}}},
        _runtime(),
    )
    complete = nodes.plan_deterministic_validation(
        {"ai_result": {"confidence_score": 1}}, _runtime()
    )
    assert conflict["validation_plan"]["outcome"] == "human_review"
    assert missing["validation_plan"]["outcome"] == "request_customer_info"
    assert complete["validation_plan"]["outcome"] == "deterministic_validation_required"


@pytest.mark.anyio
async def test_authoritative_parse_nodes_preserve_service_boundaries() -> None:
    prepare = AsyncMock(return_value={"context_id": "ctx"})
    generate = AsyncMock(return_value={"ai_available": True, "candidate_id": 9})
    adopt = AsyncMock(
        return_value={
            "email_parse_status": "parsed",
            "ticket_id": 6,
            "ticket_version": 2,
            "intent_type": "new_repair",
            "handling_level": "auto_repair",
            "confidence_score": 0.98,
        }
    )
    runtime = _runtime(
        prepare_email_parse=prepare,
        generate_ai_candidate=generate,
        adopt_email_candidate=adopt,
    )
    prepared = await external.prepare_email_parse(
        {"email_id": 1, "execution_id": "exec", "parse_request": {"force": True}}, runtime
    )
    generated = await external.generate_ai_candidate(
        {"parse_context": prepared["parse_context"]}, runtime
    )
    adopted = await external.adopt_email_candidate(
        {"parse_context": prepared["parse_context"], "ai_candidate": generated["ai_candidate"]},
        runtime,
    )
    prepare.assert_awaited_once_with({"email_id": 1, "execution_id": "exec", "force": True})
    generate.assert_awaited_once_with({"context_id": "ctx"})
    adopt.assert_awaited_once()
    assert adopted["ticket_id"] == 6
    assert adopted["ai_result"]["confidence_score"] == 0.98


@pytest.mark.anyio
async def test_validation_and_sap_nodes_return_only_state_deltas() -> None:
    validate = AsyncMock(return_value={"status": "ready_for_export", "export_id": 20})
    submit = AsyncMock(return_value={"status": "submit_unknown", "export_id": 20})
    reconcile = AsyncMock(return_value={"status": "pending", "export_id": 20})
    poll = AsyncMock(return_value={"status": "rma_received", "export_id": 20})
    runtime = _runtime(
        validate_ticket=validate,
        submit_sap=submit,
        reconcile_sap=reconcile,
        poll_sap=poll,
    )
    validated = await external.validate_ticket({"ticket_id": 7}, runtime)
    submitted = await external.submit_sap(
        {"validation_result": validated["validation_result"]}, runtime
    )
    reconciled = await external.reconcile_sap({"sap_result": submitted["sap_result"]}, runtime)
    polled = await external.poll_sap({"sap_result": reconciled["sap_result"]}, runtime)
    assert submitted["sap_submit_attempt_count"] == 1
    assert submitted["sap_submit_export_id"] == 20
    assert reconciled["sap_result"]["status"] == "pending"
    assert polled["sap_result"]["status"] == "rma_received"
    validate.assert_awaited_once_with({"ticket_id": 7, "resolving_task_id": None})
    submit.assert_awaited_once_with(20)
    reconcile.assert_awaited_once_with(20)
    poll.assert_awaited_once_with(20)


@pytest.mark.anyio
async def test_sap_submit_attempt_scope_resets_for_new_export() -> None:
    submit = AsyncMock(return_value={"status": "pending"})
    result = await external.submit_sap(
        {
            "validation_result": {"export_id": 22},
            "sap_submit_export_id": 20,
            "sap_submit_attempt_count": 2,
        },
        _runtime(submit_sap=submit),
    )

    assert result["sap_submit_export_id"] == 22
    assert result["sap_submit_attempt_count"] == 1
    submit.assert_awaited_once_with(22)


@pytest.mark.anyio
async def test_rma_and_reply_side_effect_nodes_use_persisted_ids() -> None:
    prepare_rma = AsyncMock(return_value={"status": "prepared", "reply_id": 30})
    send_rma = AsyncMock(return_value={"status": "sent", "reply_id": 30})
    archive = AsyncMock(return_value={"status": "closed", "reply_id": 30})
    prepare_reply = AsyncMock(return_value={"status": "prepared", "reply_id": 40})
    send_reply = AsyncMock(return_value={"status": "sent", "reply_id": 40})
    runtime = _runtime(
        prepare_rma=prepare_rma,
        send_rma=send_rma,
        finalize_rma_archive=archive,
        prepare_reply=prepare_reply,
        send_reply=send_reply,
    )
    prepared_rma = await external.prepare_rma(
        {"ticket_id": 7, "sap_result": {"rma_no": "RMA-1"}}, runtime
    )
    sent_rma = await external.send_rma({"rma_result": prepared_rma["rma_result"]}, runtime)
    archived = await external.finalize_rma_archive(
        {"rma_result": prepared_rma["rma_result"], "send_result": sent_rma["send_result"]},
        runtime,
    )
    prepared_reply = await external.prepare_reply(
        {"email_id": 1, "ticket_id": 7, "ai_result": {"missing_fields": {"sn": True}}},
        runtime,
    )
    sent_reply = await external.send_reply({"reply_result": prepared_reply["reply_result"]}, runtime)
    prepare_rma.assert_awaited_once_with({"ticket_id": 7, "rma_no": "RMA-1"})
    send_rma.assert_awaited_once_with(30)
    archive.assert_awaited_once_with(30)
    prepare_reply.assert_awaited_once_with(
        {"ticket_id": 7, "email_id": 1, "reply_type": "missing_fields", "missing_fields": {"sn": True}}
    )
    send_reply.assert_awaited_once_with(40)
    assert archived["archive_result"]["status"] == "closed"
    assert sent_reply["send_result"]["status"] == "sent"


@pytest.mark.anyio
async def test_human_nodes_reuse_task_and_apply_structured_decision() -> None:
    create = AsyncMock(return_value=99)
    reused = await create_human_task(
        {"adoption_result": {"manual_task_id": 88}}, _runtime(create_human_task=create)
    )
    assert reused["manual_task_id"] == 88
    create.assert_not_awaited()

    created = await create_human_task(
        {
            "execution_id": "exec",
            "email_id": 1,
            "ticket_id": 2,
            "error": {"stage": "validate_ticket", "code": "SN_NOT_FOUND"},
        },
        _runtime(create_human_task=create),
    )
    assert created["manual_task_id"] == 99
    assert create.await_args.args[0]["reasons"] == ["validate_ticket:SN_NOT_FOUND"]

    apply = AsyncMock(return_value={"status": "applied", "action": "validate"})
    applied = await apply_human_decision(
        {"ticket_id": 2, "human_result": {"task_id": 99, "action": "validate"}},
        _runtime(apply_human_decision=apply),
    )
    apply.assert_awaited_once_with({"task_id": 99, "action": "validate", "ticket_id": 2})
    assert applied["error"] is None
    assert applied["human_result"] == {
        "task_id": 99,
        "action": "validate",
        "status": "applied",
    }


def test_finish_nodes_keep_execution_state_separate_from_ticket_status() -> None:
    assert external.finish_terminal({})["workflow_outcome"] == "terminal_without_ticket_automation"
    assert external.finish_reply_delivery({"ai_result": {"missing_fields": {"sn": True}}})[
        "workflow_outcome"
    ] == "customer_followup_sent"
    assert external.finish_reply_delivery({})["workflow_outcome"] == "reply_sent"
    assert external.finish_human_resolution({"human_result": {"action": "close"}})[
        "workflow_outcome"
    ] == "human_close_completed"
    assert external.finish_completed({"archive_result": {"status": "closed"}})[
        "execution_state"
    ] == "completed"
    assert nodes.mark_shadow_validation({})["shadow_outcome"] == "deterministic_validation_required"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("node", "state", "message"),
    [
        (external.prepare_email_parse, {"email_id": 1}, "EMAIL_PARSE_SERVICE_NOT_CONFIGURED"),
        (external.validate_ticket, {"ticket_id": 1}, "VALIDATION_SERVICE_NOT_CONFIGURED"),
        (external.submit_sap, {"validation_result": {}}, "SAP_SUBMIT_SERVICE_NOT_CONFIGURED"),
        (external.send_rma, {"rma_result": {}}, "RMA_SEND_SERVICE_NOT_CONFIGURED"),
        (external.send_reply, {"reply_result": {}}, "REPLY_SEND_SERVICE_NOT_CONFIGURED"),
    ],
)
async def test_side_effect_nodes_fail_closed_without_service(node, state: dict, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        await node(state, _runtime())
