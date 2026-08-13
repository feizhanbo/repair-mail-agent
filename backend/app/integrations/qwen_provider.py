from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from app.ai.gateway import LangChainGatewayError, LangChainStructuredGateway
from app.ai.models import ModelSpec, create_chat_model
from app.integrations.ai_provider import AiJsonCompletion, AiProviderError


class QwenProvider:
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
        response_model: type[BaseModel],
        temperature: float = 0.1,
    ) -> AiJsonCompletion:
        if not self.api_key:
            raise AiProviderError("QWEN_API_KEY_NOT_CONFIGURED")
        return await self._invoke_structured(
            messages=messages,
            response_model=response_model,
            temperature=temperature,
            multimodal=False,
        )

    async def vl_chat(
        self,
        *,
        image_urls: list[str],
        prompt: str,
        response_model: type[BaseModel],
        temperature: float = 0.1,
    ) -> AiJsonCompletion:
        if not self.api_key:
            raise AiProviderError("QWEN_API_KEY_NOT_CONFIGURED")
        content_parts: list[dict[str, Any]] = []
        for url in image_urls:
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": url},
                }
            )
        content_parts.append({"type": "text", "text": prompt})

        return await self._invoke_structured(
            messages=[{"role": "user", "content": content_parts}],
            response_model=response_model,
            temperature=temperature,
            multimodal=True,
        )

    async def _invoke_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_model: type[BaseModel],
        temperature: float,
        multimodal: bool,
    ) -> AiJsonCompletion:
        spec = ModelSpec(
            provider="qwen",
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=0,
            temperature=temperature,
            max_tokens=self.max_tokens,
            structured_output_method=self.structured_output_method,
            multimodal=multimodal,
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
            completion = await gateway.invoke(messages=messages, response_model=response_model)
            return AiJsonCompletion(**completion.__dict__)
        except LangChainGatewayError as exc:
            code = {
                "TIMEOUT": "QWEN_PROVIDER_TIMEOUT",
                "EMPTY_CONTENT": "QWEN_PROVIDER_EMPTY_CONTENT",
                "OUTPUT_NOT_JSON": "QWEN_PROVIDER_OUTPUT_NOT_JSON",
                "OUTPUT_SCHEMA_INVALID": "QWEN_PROVIDER_OUTPUT_SCHEMA_INVALID",
                "AUTHENTICATION_FAILED": "QWEN_PROVIDER_AUTHENTICATION_FAILED",
                "REQUEST_FAILED": "QWEN_PROVIDER_REQUEST_FAILED",
            }.get(exc.code, f"QWEN_PROVIDER_{exc.code}")
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
