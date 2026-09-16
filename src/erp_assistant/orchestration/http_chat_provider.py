from typing import Any, cast

import httpx

from erp_assistant.orchestration.http_retrieval_providers import post_provider_json
from erp_assistant.orchestration.providers import (
    ChatMessage,
    ChatResult,
    ProviderBoundary,
)
from erp_assistant.rag.errors import RagDependencyError


class HttpChatProvider:
    production_ready = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        deployment: str,
        model: str,
        version: str,
        boundary: ProviderBoundary,
        path: str = "chat/completions",
        api_key: str = "",
    ) -> None:
        if not deployment.strip() or not model.strip() or not version.strip():
            raise ValueError("Chat profile identity is invalid")
        self._client = client
        self._path = path
        self._api_key = api_key
        self.deployment = deployment
        self.model = f"{model}:{version}"
        self.boundary = boundary

    async def generate(self, messages: list[ChatMessage]) -> ChatResult:
        payload = await post_provider_json(
            self._client,
            self._path,
            {
                "deployment": self.deployment,
                "model": self.model,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
                "response_format": {"type": "json_object"},
            },
            self._api_key,
        )
        try:
            choices = cast(list[dict[str, Any]], payload["choices"])
            message = cast(dict[str, Any], choices[0]["message"])
            content = str(message["content"])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RagDependencyError("PROVIDER_INVALID_RESPONSE", "Chat Provider 响应无效") from exc
        if not content.strip():
            raise RagDependencyError("PROVIDER_INVALID_RESPONSE", "Chat Provider 响应无效")
        return ChatResult(content=content, model=self.model)
