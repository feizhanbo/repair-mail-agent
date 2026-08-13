from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class NormalizedAttachmentContent(BaseModel):
    """Deterministic parser output before optional semantic understanding."""

    file_type: str
    parser: str
    text: str = ""
    tables: list[list[list[str]]] = Field(default_factory=list)
    embedded_image_count: int = 0
    page_count: int | None = None
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False
    semantic_mode: Literal["none", "text", "vision"] = "none"


class AttachmentParseJson(BaseModel):
    """Stable persisted result shared by deterministic and AI attachment stages."""

    schema_version: str = "attachment-parse-v2"
    file_type: str
    parser: str = "legacy"
    semantic_mode: Literal["none", "text", "vision"] = "none"
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    extracted_items: list[dict[str, Any]] = Field(default_factory=list)
    raw_text: str = ""
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False

    @field_validator("summary", "raw_text", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    @field_validator("key_points", "warnings", mode="before")
    @classmethod
    def normalize_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, default=str)
                for item in value
            ]
        return [value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)]

    @field_validator("extracted_fields", mode="before")
    @classmethod
    def normalize_fields(cls, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @field_validator("extracted_items", mode="before")
    @classmethod
    def normalize_items(cls, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @field_validator("truncated", mode="before")
    @classmethod
    def normalize_truncated(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "是"}
        return bool(value)
