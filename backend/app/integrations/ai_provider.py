from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError


class AiProviderError(RuntimeError):
    """Raised when an AI provider call fails before a valid schema is returned."""


class AiExtractResponse(BaseModel):
    intent_type: str = "unknown"
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    extracted_items: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: dict[str, Any] = Field(default_factory=dict)
    conflict_fields: dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(default=0, ge=0, le=1)
    field_confidences: dict[str, float] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence_reasons: list[str] = Field(default_factory=list)
    manual_review_direction: str | None = None
    original_evidence: list[str] = Field(default_factory=list)


class AiReplyDraftResponse(BaseModel):
    subject: str = ""
    body: str = ""
    missing_fields: dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(default=0, ge=0, le=1)
    risk_level: str = "low"
    suggestions: list[str] = Field(default_factory=list)


T = TypeVar("T", bound=BaseModel)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {str(item): "需要补充" for item in value if isinstance(item, (str, int, float))}
    return {}


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return []


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 1 < number <= 100:
        number /= 100
    return max(0.0, min(1.0, number))


def _normalize_response_payload(payload: Any, response_model: type[BaseModel]) -> Any:
    if not isinstance(payload, dict):
        return payload
    normalized = dict(payload)
    if response_model is AiExtractResponse:
        for field in ("extracted_fields", "missing_fields", "conflict_fields", "evidence"):
            normalized[field] = _as_mapping(normalized.get(field))
        items = normalized.get("extracted_items")
        if isinstance(items, dict):
            items = items.get("items") if isinstance(items.get("items"), list) else [items]
        normalized["extracted_items"] = items if isinstance(items, list) else []
        normalized["confidence_score"] = _confidence(normalized.get("confidence_score"))
        normalized["field_confidences"] = {
            str(key): _confidence(value)
            for key, value in _as_mapping(normalized.get("field_confidences")).items()
        }
        normalized["confidence_reasons"] = _as_string_list(normalized.get("confidence_reasons"))
        normalized["original_evidence"] = _as_string_list(normalized.get("original_evidence"))
        direction = normalized.get("manual_review_direction")
        normalized["manual_review_direction"] = str(direction) if direction is not None else None
    elif response_model is AiReplyDraftResponse:
        normalized["missing_fields"] = _as_mapping(normalized.get("missing_fields"))
        normalized["confidence_score"] = _confidence(normalized.get("confidence_score"))
        normalized["suggestions"] = _as_string_list(normalized.get("suggestions"))
    return normalized


@dataclass
class AiJsonCompletion(Generic[T]):
    trace_id: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    output_text: str
    parsed: T
    latency_ms: int


class DeepSeekProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def chat_json(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[T],
        temperature: float = 0.1,
    ) -> AiJsonCompletion[T]:
        if not self.api_key:
            raise AiProviderError("AI_API_KEY_NOT_CONFIGURED")

        trace_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                response_payload = response.json()
        except httpx.TimeoutException as exc:
            raise AiProviderError("AI_PROVIDER_TIMEOUT") from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            raise AiProviderError(f"AI_PROVIDER_HTTP_{status_code}") from exc
        except httpx.HTTPError as exc:
            raise AiProviderError("AI_PROVIDER_REQUEST_FAILED") from exc
        except ValueError as exc:
            raise AiProviderError("AI_PROVIDER_INVALID_RESPONSE_JSON") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        output_text = _extract_content(response_payload)
        try:
            parsed_json = _normalize_response_payload(json.loads(output_text), response_model)
            parsed = response_model.model_validate(parsed_json)
        except json.JSONDecodeError as exc:
            raise AiProviderError("AI_PROVIDER_OUTPUT_NOT_JSON") from exc
        except ValidationError as exc:
            error = AiProviderError("AI_PROVIDER_OUTPUT_SCHEMA_INVALID")
            error.validation_error_types = [item["type"] for item in exc.errors()]  # type: ignore[attr-defined]
            raise error from exc

        return AiJsonCompletion(
            trace_id=trace_id,
            request_payload=payload,
            response_payload=response_payload,
            output_text=output_text,
            parsed=parsed,
            latency_ms=latency_ms,
        )


def _extract_content(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiProviderError("AI_PROVIDER_EMPTY_CHOICES")
    first = choices[0]
    if not isinstance(first, dict):
        raise AiProviderError("AI_PROVIDER_INVALID_CHOICE")
    message = first.get("message")
    if not isinstance(message, dict):
        raise AiProviderError("AI_PROVIDER_INVALID_MESSAGE")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AiProviderError("AI_PROVIDER_EMPTY_CONTENT")
    return content.strip()
