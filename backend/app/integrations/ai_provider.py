from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import httpx
from pydantic import BaseModel, Field

from app.ai.gateway import LangChainGatewayError, LangChainStructuredGateway
from app.ai.models import ModelSpec, create_chat_model
from app.core.repair_items import normalize_repair_item
from app.core.email_classification import CLASSIFICATION_VERSION, INTENT_LEVEL, decision_for_intent


class AiProviderError(RuntimeError):
    """Raised when an AI provider call fails before a valid schema is returned."""


class AiExtractResponse(BaseModel):
    intent_type: str = "unknown"
    intent_subtype: str | None = None
    handling_level: str | None = None
    classification_version: str = CLASSIFICATION_VERSION
    classification_reason_code: str | None = None
    candidate_intents: list[str] = Field(default_factory=list)
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


_INTENT_ALIASES = {"customer_reply": "customer_supplement", "internal_forward": "repair_thread_other", "normal_reply": "repair_thread_other", "device_received": "device_intake_received"}
_ALLOWED_INTENTS = set(INTENT_LEVEL) | {"irrelevant"}
_ALLOWED_IRRELEVANT_SUBTYPES = {"general_irrelevant", "out_of_scope_repair"}


def _without_optional_phone_requirement(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if str(key) not in {"contact_phone", "phone"}}


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
        intent = str(normalized.get("intent_type") or "unknown").strip().lower()
        mapped_intent = _INTENT_ALIASES.get(intent, intent)
        normalized["intent_type"] = mapped_intent if mapped_intent in _ALLOWED_INTENTS else "unknown"
        subtype = str(normalized.get("intent_subtype") or "").strip().lower()
        if normalized["intent_type"] == "irrelevant":
            normalized["intent_subtype"] = (
                subtype if subtype in _ALLOWED_IRRELEVANT_SUBTYPES else "general_irrelevant"
            )
        else:
            normalized["intent_subtype"] = None
        if normalized["intent_type"] == "irrelevant":
            normalized["handling_level"] = None
            normalized["classification_reason_code"] = normalized.get("classification_reason_code") or "AI_MAILBOX_IRRELEVANT"
        else:
            decision = decision_for_intent(normalized["intent_type"], reason_code="AI_SEMANTIC_CANDIDATE")
            normalized["handling_level"] = decision.handling_level
            normalized["classification_reason_code"] = normalized.get("classification_reason_code") or decision.reason_code
        normalized["classification_version"] = CLASSIFICATION_VERSION
        normalized["candidate_intents"] = [
            str(_INTENT_ALIASES.get(str(item).strip().lower(), str(item).strip().lower()))
            for item in normalized.get("candidate_intents", [])
            if str(_INTENT_ALIASES.get(str(item).strip().lower(), str(item).strip().lower())) in INTENT_LEVEL
        ]
        for field in ("extracted_fields", "missing_fields", "conflict_fields", "evidence"):
            normalized[field] = _as_mapping(normalized.get(field))
        normalized["missing_fields"] = _without_optional_phone_requirement(normalized["missing_fields"])
        normalized["conflict_fields"] = _without_optional_phone_requirement(normalized["conflict_fields"])
        items = normalized.get("extracted_items")
        if isinstance(items, dict):
            items = items.get("items") if isinstance(items.get("items"), list) else [items]
        normalized_items: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            normalized_item = normalize_repair_item(item)
            normalized_items.append(normalized_item)
        normalized["extracted_items"] = normalized_items
        normalized["confidence_score"] = _confidence(normalized.get("confidence_score"))
        normalized["field_confidences"] = _without_optional_phone_requirement({
            str(key): _confidence(value)
            for key, value in _as_mapping(normalized.get("field_confidences")).items()
        })
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
        max_tokens: int | None = None,
        structured_output_method: str = "json_mode",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.structured_output_method = structured_output_method
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
        spec = ModelSpec(
            provider="deepseek",
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=0,
            temperature=temperature,
            max_tokens=self.max_tokens,
            structured_output_method=self.structured_output_method,
        )
        client = (
            httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport)
            if self.transport is not None
            else None
        )
        try:
            gateway = LangChainStructuredGateway(
                create_chat_model(spec, async_client=client),
                spec,
            )
            completion = await gateway.invoke(
                messages=messages,
                response_model=response_model,
                normalize=_normalize_response_payload,
            )
            return AiJsonCompletion(**completion.__dict__)
        except LangChainGatewayError as exc:
            code = {
                "TIMEOUT": "AI_PROVIDER_TIMEOUT",
                "EMPTY_CONTENT": "AI_PROVIDER_EMPTY_CONTENT",
                "OUTPUT_NOT_JSON": "AI_PROVIDER_OUTPUT_NOT_JSON",
                "OUTPUT_SCHEMA_INVALID": "AI_PROVIDER_OUTPUT_SCHEMA_INVALID",
                "AUTHENTICATION_FAILED": "AI_PROVIDER_AUTHENTICATION_FAILED",
                "REQUEST_FAILED": "AI_PROVIDER_REQUEST_FAILED",
            }.get(exc.code, f"AI_PROVIDER_{exc.code}")
            error = AiProviderError(code)
            for name in (
                "raw_output", "trace_id", "request_payload", "response_payload",
                "latency_ms", "validation_error_types",
            ):
                value = getattr(exc, name, None)
                if value not in (None, []):
                    setattr(error, name, value)
            raise error from exc
        finally:
            if client is not None:
                await client.aclose()
