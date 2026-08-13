from __future__ import annotations

from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models import WorkflowExecution, WorkflowInterrupt
from app.workflows.checkpoint import postgres_checkpointer
from app.workflows.email_ticket import runner


class RecoveryState(TypedDict, total=False):
    execution_id: str
    prepared: bool
    approved: bool
    completed: bool


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _build_recovery_graph(*, checkpointer, prepare_calls: list[str]):
    async def prepare(_state: RecoveryState) -> dict:
        prepare_calls.append("prepare")
        return {"prepared": True}

    def wait_for_review(_state: RecoveryState) -> dict:
        response = interrupt({"kind": "checkpoint_smoke_review"})
        return {"approved": bool(response.get("approved"))}

    async def finish(state: RecoveryState) -> dict:
        assert state["prepared"] is True
        assert state["approved"] is True
        return {"completed": True}

    builder = StateGraph(RecoveryState)
    builder.add_node("prepare", prepare)
    builder.add_node("wait_for_review", wait_for_review)
    builder.add_node("finish", finish)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "wait_for_review")
    builder.add_edge("wait_for_review", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


@pytest.mark.anyio
async def test_postgres_checkpoint_survives_new_connection_and_graph_rebuild() -> None:
    database_url = settings.LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL.strip()
    if not database_url:
        pytest.skip("LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL is not configured")
    parsed = make_url(database_url)
    assert parsed.host in {"127.0.0.1", "localhost"}, "checkpoint smoke must use localhost"
    assert str(parsed.database or "").endswith("_test"), "checkpoint smoke database must end in _test"

    thread_id = f"checkpoint-smoke-{uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}
    prepare_calls: list[str] = []
    try:
        async with postgres_checkpointer(database_url, setup=True, strict_msgpack=True) as saver:
            first_graph = _build_recovery_graph(checkpointer=saver, prepare_calls=prepare_calls)
            interrupted = await first_graph.ainvoke({}, config)
            assert interrupted["__interrupt__"][0].value == {"kind": "checkpoint_smoke_review"}
            assert prepare_calls == ["prepare"]

        # A new connection and newly compiled graph simulate service restart.
        async with postgres_checkpointer(database_url, strict_msgpack=True) as saver:
            rebuilt_graph = _build_recovery_graph(checkpointer=saver, prepare_calls=prepare_calls)
            completed = await rebuilt_graph.ainvoke(Command(resume={"approved": True}), config)
            assert completed["completed"] is True
            assert prepare_calls == ["prepare"]
    finally:
        async with postgres_checkpointer(database_url, strict_msgpack=True) as saver:
            await saver.adelete_thread(thread_id)


@pytest.mark.anyio
async def test_mysql_ledger_and_postgres_checkpoint_reconcile_across_wait_and_completion() -> None:
    checkpoint_url = settings.LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL.strip()
    mysql_url = settings.DB_SMOKE_DATABASE_URL.strip()
    if not checkpoint_url:
        pytest.skip("LANGGRAPH_CHECKPOINT_SMOKE_DATABASE_URL is not configured")
    if not mysql_url:
        pytest.skip("DB_SMOKE_DATABASE_URL is not configured")

    checkpoint_target = make_url(checkpoint_url)
    assert checkpoint_target.host in {"127.0.0.1", "localhost"}
    assert str(checkpoint_target.database or "").endswith("_test")
    mysql_target = make_url(mysql_url)
    assert mysql_target.host in {"127.0.0.1", "localhost"}
    assert mysql_target.drivername.startswith("mysql+")
    assert mysql_target.database == "repair_system_test"
    if mysql_target.drivername == "mysql+aiomysql":
        mysql_target = mysql_target.set(drivername="mysql+asyncmy")
        mysql_url = mysql_target.render_as_string(hide_password=False)

    suffix = uuid4().hex
    execution_id = f"cross-store-{suffix}"
    thread_id = f"cross-store-thread-{suffix}"
    config = {"configurable": {"thread_id": thread_id}}
    engine = create_async_engine(mysql_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        prepare_calls: list[str] = []
        async with postgres_checkpointer(checkpoint_url, setup=True, strict_msgpack=True) as saver:
            graph = _build_recovery_graph(checkpointer=saver, prepare_calls=prepare_calls)
            interrupted = await graph.ainvoke({"execution_id": execution_id}, config)
            waiting_snapshot = await graph.aget_state(config)
            checkpoint_id = runner._snapshot_checkpoint_id(waiting_snapshot)
            checkpoint_step = runner._snapshot_checkpoint_step(waiting_snapshot)
            graph_interrupt = interrupted["__interrupt__"][0]
            assert checkpoint_id is not None
            assert checkpoint_step is not None

            async with sessions() as session:
                execution = WorkflowExecution(
                    execution_id=execution_id,
                    graph_thread_id=thread_id,
                    workflow_name="email_ticket",
                    workflow_version="langgraph-v2",
                    state_schema_version="email-ticket-state-v1",
                    execution_mode="langgraph",
                    status="waiting_human",
                    checkpoint_id=checkpoint_id,
                    checkpoint_step=checkpoint_step,
                )
                session.add(execution)
                session.add(
                    WorkflowInterrupt(
                        execution_id=execution_id,
                        interrupt_id=str(graph_interrupt.id),
                        checkpoint_id=checkpoint_id,
                        checkpoint_step=checkpoint_step,
                        status="pending",
                        request_payload=dict(graph_interrupt.value),
                    )
                )
                await session.commit()

                waiting = await runner._verified_stable_execution_result(
                    session,
                    execution,
                    waiting_snapshot,
                )
                assert waiting["__interrupt__"][0].id == graph_interrupt.id

        # Reopen both stores and rebuild the Graph to model a process restart.
        async with postgres_checkpointer(checkpoint_url, strict_msgpack=True) as saver:
            rebuilt_graph = _build_recovery_graph(
                checkpointer=saver,
                prepare_calls=prepare_calls,
            )
            completed = await rebuilt_graph.ainvoke(
                Command(resume={"approved": True}),
                config,
            )
            completed_snapshot = await rebuilt_graph.aget_state(config)
            async with sessions() as session:
                execution = await session.scalar(
                    select(WorkflowExecution).where(
                        WorkflowExecution.execution_id == execution_id
                    )
                )
                assert execution is not None
                execution.status = "completed"
                execution.checkpoint_id = runner._snapshot_checkpoint_id(completed_snapshot)
                execution.checkpoint_step = runner._snapshot_checkpoint_step(completed_snapshot)
                ledger = await session.scalar(
                    select(WorkflowInterrupt).where(
                        WorkflowInterrupt.execution_id == execution_id
                    )
                )
                assert ledger is not None
                ledger.status = "resumed"
                await session.commit()

                terminal = await runner._verified_stable_execution_result(
                    session,
                    execution,
                    completed_snapshot,
                )
                assert terminal["completed"] is True
                assert completed["completed"] is True
                assert prepare_calls == ["prepare"]
    finally:
        async with sessions() as session:
            await session.execute(
                delete(WorkflowExecution).where(WorkflowExecution.execution_id == execution_id)
            )
            await session.commit()
        await engine.dispose()
        async with postgres_checkpointer(checkpoint_url, strict_msgpack=True) as saver:
            await saver.adelete_thread(thread_id)
