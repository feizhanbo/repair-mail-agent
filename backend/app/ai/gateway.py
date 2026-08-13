from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from app.ai.models import ModelSpec


T = TypeVar("T", bound=BaseModel)


class LangChainGatewayError(RuntimeError):
    """A provider-independent model invocation or structured-output failure."""

    def __init__(self, code: str, *, cause: Exception | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.cause = cause
        self.raw_output: str | None = None
        self.trace_id: str | None = None
        self.request_payload: dict[str, Any] | None = None
        self.response_payload: dict[str, Any] | None = None
        self.latency_ms: int | None = None
        self.validation_error_types: list[str] = []


@dataclass
class StructuredCompletion(Generic[T]):
    trace_id: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    output_text: str
    parsed: T
    latency_ms: int


def _message_text(message: AIMessage | None) -> str:
    if message is None:
        return ""
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False, default=str)


def _usage_payload(message: AIMessage | None) -> dict[str, Any]:
    if message is None:
        return {}
    usage = dict(getattr(message, "usage_metadata", None) or {})
    if not usage:
        token_usage = (message.response_metadata or {}).get("token_usage")
        if isinstance(token_usage, dict):
            usage = token_usage
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and isinstance(input_tokens, int) and isinstance(output_tokens, int):
        total_tokens = input_tokens + output_tokens
    return {
        key: value
        for key, value in {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_tokens,
        }.items()
        if value is not None
    }


def _error_code(exc: Exception) -> str:
    name = exc.__class__.__name__.upper()
    text = str(exc).upper()
    if "TIMEOUT" in name or "TIMEOUT" in text:
        return "TIMEOUT"
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return f"HTTP_{status_code}"
    if "API KEY" in text or "AUTHENTICATION" in name:
        return "AUTHENTICATION_FAILED"
    return "REQUEST_FAILED"


class LangChainStructuredGateway:
    def __init__(self, model: BaseChatModel, spec: ModelSpec) -> None:
        self.model = model
        self.spec = spec

    async def invoke(
        self,
        *,
        messages: list[dict[str, Any]],
        response_model: type[T],
        normalize: Callable[[Any, type[BaseModel]], Any] | None = None,
        method: str | None = None,
    ) -> StructuredCompletion[T]:
        trace_id = uuid.uuid4().hex
        request_payload = {
            "provider": self.spec.provider,
            "model": self.spec.model,
            "messages": messages,
            "temperature": self.spec.temperature,
            "structured_output_method": method or self.spec.structured_output_method,
            "response_schema": response_model.model_json_schema(),
        }
        started = time.perf_counter()
        try:
            structured_model = self.model.with_structured_output(
                response_model.model_json_schema(),
                method=method or self.spec.structured_output_method,
                include_raw=True,
            )
            result = await structured_model.ainvoke(messages)
        except Exception as exc:
            error = LangChainGatewayError(_error_code(exc), cause=exc)
            error.trace_id = trace_id
            error.request_payload = request_payload
            error.latency_ms = int((time.perf_counter() - started) * 1000)
            raise error from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        if not isinstance(result, dict):
            result = {"raw": None, "parsed": result, "parsing_error": None}
        raw = result.get("raw") if isinstance(result.get("raw"), AIMessage) else None
        parsed_payload = result.get("parsed")
        parsing_error = result.get("parsing_error")
        output_text = _message_text(raw)
        response_payload = {
            "id": getattr(raw, "id", None),
            "model": (raw.response_metadata or {}).get("model_name") if raw else None,
            "finish_reason": (raw.response_metadata or {}).get("finish_reason") if raw else None,
            "usage": _usage_payload(raw),
        }

        if raw is not None and not output_text.strip():
            error = LangChainGatewayError("EMPTY_CONTENT")
            error.raw_output = output_text
            error.trace_id = trace_id
            error.request_payload = request_payload
            error.response_payload = response_payload
            error.latency_ms = latency_ms
            raise error
        if parsing_error is not None or parsed_payload is None:
            error = LangChainGatewayError(
                "OUTPUT_NOT_JSON" if parsing_error and "json" in str(parsing_error).lower() else "OUTPUT_SCHEMA_INVALID",
                cause=parsing_error if isinstance(parsing_error, Exception) else None,
            )
            error.raw_output = output_text
            error.trace_id = trace_id
            error.request_payload = request_payload
            error.response_payload = response_payload
            error.latency_ms = latency_ms
            raise error

        try:
            normalized = normalize(parsed_payload, response_model) if normalize else parsed_payload
            parsed = normalized if isinstance(normalized, response_model) else response_model.model_validate(normalized)
        except ValidationError as exc:
            error = LangChainGatewayError("OUTPUT_SCHEMA_INVALID", cause=exc)
            error.validation_error_types = [item["type"] for item in exc.errors()]
            error.raw_output = output_text
            error.trace_id = trace_id
            error.request_payload = request_payload
            error.response_payload = response_payload
            error.latency_ms = latency_ms
            raise error from exc

        return StructuredCompletion(
            trace_id=trace_id,
            request_payload=request_payload,
            response_payload=response_payload,
            output_text=output_text,
            parsed=parsed,
            latency_ms=latency_ms,
        )
