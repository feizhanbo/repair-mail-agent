from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


@asynccontextmanager
async def postgres_checkpointer(
    connection_url: str,
    *,
    setup: bool = False,
    strict_msgpack: bool = True,
) -> AsyncIterator[BaseCheckpointSaver]:
    """Create a dedicated PostgreSQL checkpointer; never falls back to business MySQL."""
    if not connection_url.strip():
        raise ValueError("LANGGRAPH_CHECKPOINT_DATABASE_URL_NOT_CONFIGURED")
    serde = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=None if strict_msgpack else True,
    )
    async with AsyncPostgresSaver.from_conn_string(connection_url, serde=serde) as saver:
        if setup:
            await saver.setup()
        yield saver
