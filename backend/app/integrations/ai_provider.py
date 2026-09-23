from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from app.ai.schemas import StrictAiModel
from app.core.email_classification import CLASSIFICATION_VERSION


class AiProviderError(RuntimeError):
    """Raised when an AI provider call fails before a valid schema is returned."""


class NormalizedRepairCandidate(BaseModel):
    """Backend-owned normalized candidate; never used as an LLM response schema."""

    intent_type: str = "unknown"
    handling_level: str | None = None
    classification_version: str = CLASSIFICATION_VERSION
    classification_reason_code: str | None = None
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    extracted_items: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: dict[str, Any] = Field(default_factory=dict)
    conflict_fields: dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(default=0, ge=0, le=1)
    field_confidences: dict[str, float] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    manual_review_direction: str | None = None


class AiReplyDraftResponse(StrictAiModel):
    subject: str
    body: str
    missing_fields: dict[str, Any]
    confidence_score: float = Field(ge=0, le=1)
    risk_level: str
    suggestions: list[str]


T = TypeVar("T", bound=BaseModel)


@dataclass
class AiJsonCompletion(Generic[T]):
    trace_id: str
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    output_text: str
    parsed: T
    latency_ms: int
    task: str | None = None
    route_name: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    route_attempt: int = 1
    fallback_used: bool = False
    structured_output_method: str | None = None
