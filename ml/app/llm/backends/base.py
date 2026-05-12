"""Backend protocol — `GeminiBackend` and `PolzaBackend` both
satisfy it via duck typing. Formalising the surface as a `Protocol`
gives `LLMClient.backend: Backend` proper type annotations and lets
tests assert `isinstance(b, Backend)` if they want."""

from typing import Any, Protocol, runtime_checkable

import httpx

from app.llm.errors import LLMClientError


@runtime_checkable
class Backend(Protocol):
    model: str
    max_output_tokens: int
    http_client: httpx.AsyncClient  # tests monkey-patch this attribute

    async def count_tokens(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> int: ...

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> tuple[str, int, int]: ...

    def translate_http_error(self, exc: httpx.HTTPStatusError) -> LLMClientError: ...

    async def close(self) -> None: ...
