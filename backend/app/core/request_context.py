from __future__ import annotations

import re
from contextvars import ContextVar, Token
from uuid import uuid4


_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)
user_agent_var: ContextVar[str | None] = ContextVar("user_agent", default=None)


def normalize_correlation_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and _SAFE_CORRELATION_ID.fullmatch(candidate):
        return candidate
    return uuid4().hex


def bind_request_context(
    *, correlation_id: str, client_ip: str | None, user_agent: str | None
) -> tuple[Token, Token, Token]:
    return (
        correlation_id_var.set(correlation_id),
        client_ip_var.set(client_ip),
        user_agent_var.set((user_agent or "")[:500] or None),
    )


def reset_request_context(tokens: tuple[Token, Token, Token]) -> None:
    correlation_id_var.reset(tokens[0])
    client_ip_var.reset(tokens[1])
    user_agent_var.reset(tokens[2])


def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def get_client_ip() -> str | None:
    return client_ip_var.get()


def get_user_agent() -> str | None:
    return user_agent_var.get()
