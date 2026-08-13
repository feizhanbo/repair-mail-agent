from __future__ import annotations

import asyncio

from app.config import settings
from app.workflows.checkpoint import postgres_checkpointer


async def _setup() -> None:
    """Initialize only the dedicated PostgreSQL LangGraph checkpoint schema."""
    async with postgres_checkpointer(
        settings.LANGGRAPH_CHECKPOINT_DATABASE_URL,
        setup=True,
        strict_msgpack=settings.LANGGRAPH_STRICT_MSGPACK,
    ):
        pass


def main() -> None:
    asyncio.run(_setup())


if __name__ == "__main__":
    main()
