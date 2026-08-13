from __future__ import annotations

import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict


class WorkflowError(TypedDict, total=False):
    code: str
    stage: str
    retryable: bool
    recoverable: bool


class EmailTicketState(TypedDict, total=False):
    execution_id: str
    graph_thread_id: str
    email_id: int
    thread_id: int | None
    ticket_id: int | None
    ticket_status_snapshot: str | None
    ticket_version_snapshot: int | None
    execution_state: str
    email_snapshot: dict[str, Any]
    normalized_content: dict[str, Any]
    attachment_results: list[dict[str, Any]]
    ai_result: dict[str, Any]
    business_context: dict[str, Any]
    validation_plan: dict[str, Any]
    manual_task_id: int
    human_result: dict[str, Any]
    validation_result: dict[str, Any]
    sap_result: dict[str, Any]
    sap_submit_attempt_count: int
    sap_submit_export_id: int
    rma_result: dict[str, Any]
    send_result: dict[str, Any]
    archive_result: dict[str, Any]
    reply_result: dict[str, Any]
    parse_context: dict[str, Any]
    ai_candidate: dict[str, Any]
    adoption_result: dict[str, Any]
    parse_request: dict[str, Any]
    workflow_outcome: str
    shadow_outcome: str
    error: WorkflowError | None
    route_history: Annotated[list[str], operator.add]


LoadEmail = Callable[[int], Awaitable[dict[str, Any]]]
ClassifyEmail = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
CreateHumanTask = Callable[[dict[str, Any]], Awaitable[int]]
ValidateTicket = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SubmitSap = Callable[[int], Awaitable[dict[str, Any]]]
ReconcileSap = Callable[[int], Awaitable[dict[str, Any]]]
PollSap = Callable[[int], Awaitable[dict[str, Any]]]
PrepareRma = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SendRma = Callable[[int], Awaitable[dict[str, Any]]]
SendReply = Callable[[int], Awaitable[dict[str, Any]]]
FinalizeRmaArchive = Callable[[int], Awaitable[dict[str, Any]]]
PrepareReply = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
PrepareEmailParse = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
GenerateAiCandidate = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
AdoptEmailCandidate = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ApplyHumanDecision = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
RecordNodeEvent = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class EmailTicketRuntime:
    load_email: LoadEmail
    classify_email: ClassifyEmail | None = None
    create_human_task: CreateHumanTask | None = None
    validate_ticket: ValidateTicket | None = None
    submit_sap: SubmitSap | None = None
    reconcile_sap: ReconcileSap | None = None
    poll_sap: PollSap | None = None
    prepare_rma: PrepareRma | None = None
    send_rma: SendRma | None = None
    send_reply: SendReply | None = None
    finalize_rma_archive: FinalizeRmaArchive | None = None
    prepare_reply: PrepareReply | None = None
    prepare_email_parse: PrepareEmailParse | None = None
    generate_ai_candidate: GenerateAiCandidate | None = None
    adopt_email_candidate: AdoptEmailCandidate | None = None
    apply_human_decision: ApplyHumanDecision | None = None
    record_node_event: RecordNodeEvent | None = None
    auto_apply_min_confidence: float = 0.85
