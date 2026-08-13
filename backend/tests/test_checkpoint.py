from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.workflows import checkpoint


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_postgres_checkpointer_uses_explicit_strict_serializer(monkeypatch) -> None:
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_from_conn_string(connection_url: str, *, serde):
        captured["url"] = connection_url
        captured["serde"] = serde
        yield SimpleNamespace(setup=None)

    monkeypatch.setattr(
        checkpoint.AsyncPostgresSaver,
        "from_conn_string",
        fake_from_conn_string,
    )

    async with checkpoint.postgres_checkpointer(
        "postgresql://checkpoint",
        strict_msgpack=True,
    ):
        pass

    assert captured["url"] == "postgresql://checkpoint"
    assert captured["serde"]._allowed_msgpack_modules is None
    assert captured["serde"].pickle_fallback is False


@pytest.mark.anyio
async def test_postgres_checkpointer_setup_is_explicit(monkeypatch) -> None:
    setup_calls = 0

    class FakeSaver:
        async def setup(self) -> None:
            nonlocal setup_calls
            setup_calls += 1

    @asynccontextmanager
    async def fake_from_conn_string(_connection_url: str, *, serde):
        del serde
        yield FakeSaver()

    monkeypatch.setattr(
        checkpoint.AsyncPostgresSaver,
        "from_conn_string",
        fake_from_conn_string,
    )

    async with checkpoint.postgres_checkpointer(
        "postgresql://checkpoint",
        setup=True,
    ):
        pass

    assert setup_calls == 1
