from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI


class ModelCapability(StrEnum):
    EMAIL_CLASSIFICATION_EXTRACTION = "email_classification_extraction"
    ATTACHMENT_TEXT_UNDERSTANDING = "attachment_text_understanding"
    ATTACHMENT_VISION = "attachment_vision"
    REPLY_DRAFT = "reply_draft"


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_seconds: float
    max_retries: int = 0
    temperature: float = 0.1
    max_tokens: int | None = None
    structured_output_method: str = "json_mode"
    multimodal: bool = False


def create_chat_model(
    spec: ModelSpec,
    *,
    async_client: httpx.AsyncClient | None = None,
) -> BaseChatModel:
    """Create the LangChain chat model used by all OpenAI-compatible providers."""
    if not spec.api_key:
        raise ValueError("AI_API_KEY_NOT_CONFIGURED")
    if not spec.model:
        raise ValueError("AI_MODEL_NOT_CONFIGURED")
    if not spec.base_url:
        raise ValueError("AI_BASE_URL_NOT_CONFIGURED")

    kwargs: dict[str, object] = {
        "model": spec.model,
        "api_key": spec.api_key,
        "base_url": spec.base_url.rstrip("/"),
        "timeout": spec.timeout_seconds,
        "max_retries": spec.max_retries,
        "temperature": spec.temperature,
    }
    if spec.max_tokens is not None:
        kwargs["max_completion_tokens"] = spec.max_tokens
    if async_client is not None:
        kwargs["http_async_client"] = async_client
    return ChatOpenAI(**kwargs)
