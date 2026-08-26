from __future__ import annotations

import re
from contextvars import ContextVar, Token
from uuid import uuid4


_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)
user_agent_var: ContextVar[str | None] = ContextVar("user_agent", default=None)
user_id_var: ContextVar[int | None] = ContextVar("user_id", default=None)
job_run_id_var: ContextVar[int | None] = ContextVar("job_run_id", default=None)


def normalize_correlation_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and _SAFE_CORRELATION_ID.fullmatch(candidate):
        return candidate
    return uuid4().hex


def normalize_request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return f"req_{uuid4().hex}"


def bind_request_context(
    *, request_id: str | None = None, correlation_id: str, client_ip: str | None, user_agent: str | None,
    user_id: int | None = None, job_run_id: int | None = None,
) -> tuple[Token, ...]:
    return (
        request_id_var.set(request_id),
        correlation_id_var.set(correlation_id),
        client_ip_var.set(client_ip),
        user_agent_var.set((user_agent or "")[:500] or None),
        user_id_var.set(user_id),
        job_run_id_var.set(job_run_id),
    )


def reset_request_context(tokens: tuple[Token, ...]) -> None:
    request_id_var.reset(tokens[0])
    correlation_id_var.reset(tokens[1])
    client_ip_var.reset(tokens[2])
    user_agent_var.reset(tokens[3])
    user_id_var.reset(tokens[4])
    job_run_id_var.reset(tokens[5])


def get_request_id() -> str | None:
    return request_id_var.get()


def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def get_client_ip() -> str | None:
    return client_ip_var.get()


def get_user_agent() -> str | None:
    return user_agent_var.get()


def get_user_id() -> int | None:
    return user_id_var.get()


def set_user_id(user_id: int | None) -> None:
    user_id_var.set(user_id)


def get_job_run_id() -> int | None:
    return job_run_id_var.get()
