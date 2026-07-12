from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class AiExtractFields(BaseModel):
    intent_type: str = "unknown"
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    extracted_items: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: dict[str, Any] = Field(default_factory=dict)
    conflict_fields: dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(default=0, ge=0, le=1)
    field_confidences: dict[str, float] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence_reasons: list[str] = Field(default_factory=list)


def validate_ai_result(raw_output: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        return None, f"JSON解析失败: {exc}"
    try:
        validated = AiExtractFields(**data)
        return validated.model_dump(), None
    except ValidationError as exc:
        return None, f"Schema校验失败: {exc}"
