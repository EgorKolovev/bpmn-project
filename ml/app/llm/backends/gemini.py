"""Direct Google Gemini API backend."""

from typing import Any

import httpx

from app.config import GEMINI_THINKING_BUDGET, LLM_HTTP_TIMEOUT
from app.llm.errors import LLMClientError

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiBackend:
    """Direct Google Gemini API backend."""

    def __init__(self, api_key: str, model: str, max_output_tokens: int):
        self.model = model
        self.max_output_tokens = max_output_tokens
        # Timeout is env-configurable (LLM_HTTP_TIMEOUT, default 240s) so a
        # complex request with thinking + long BPMN output completes without
        # an HTTP-level ReadTimeout. See config.LLM_HTTP_TIMEOUT.
        self.http_client = httpx.AsyncClient(
            timeout=LLM_HTTP_TIMEOUT,
            headers={"x-goog-api-key": api_key},
        )

    def _build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
        thinking_budget: int | None = None,
    ) -> dict[str, Any]:
        # Per-call override (edit/classify pass a lower budget than
        # generate) → falls back to the module default.
        budget = thinking_budget if thinking_budget is not None else GEMINI_THINKING_BUDGET
        generation_config: dict[str, Any] = {
            "temperature": 0.2,
            "maxOutputTokens": self.max_output_tokens,
            "responseMimeType": "application/json",
            # Thinking budget — 0 off / -1 dynamic / >0 cap. HARD cap on
            # Gemini. See config.GEMINI_THINKING_BUDGET. Ignored by
            # non-thinking models.
            "thinkingConfig": {"thinkingBudget": budget},
        }
        if response_schema is not None:
            # Constrained decoding — forces the model to emit JSON matching
            # the schema. Critical for long XML payloads where free-form
            # JSON output from flash-lite occasionally double-escapes.
            generation_config["responseJsonSchema"] = response_schema
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": generation_config,
        }

    async def count_tokens(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> int:
        payload = self._build_payload(system_prompt, user_prompt, response_schema)
        # countTokens requires `model` inside the nested generate_content_request
        count_request = {
            "generateContentRequest": {
                "model": f"models/{self.model}",
                **payload,
            }
        }
        response = await self.http_client.post(
            f"{GEMINI_API_URL}/{self.model}:countTokens",
            json=count_request,
        )
        response.raise_for_status()
        data = response.json()
        total_tokens = data.get("totalTokens")
        if total_tokens is None:
            raise LLMClientError("Gemini API did not return token counts.")
        return int(total_tokens)

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
        thinking_budget: int | None = None,
    ) -> tuple[str, int, int]:
        """Returns (text, prompt_tokens, output_tokens)."""
        payload = self._build_payload(system_prompt, user_prompt, response_schema, thinking_budget)
        response = await self.http_client.post(
            f"{GEMINI_API_URL}/{self.model}:generateContent",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        if "candidates" not in data or not data["candidates"]:
            raise LLMClientError("Gemini API returned no completion.")

        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason", "")
        # MAX_TOKENS — silent truncation; flip to LLMClientError so the
        # operator learns to bump MAX_OUTPUT_TOKENS or thinking budget.
        if finish_reason == "MAX_TOKENS":
            usage = data.get("usageMetadata", {})
            raise LLMClientError(
                f"Gemini truncated response at MAX_TOKENS. "
                f"usage={usage}. Increase GEMINI_MAX_OUTPUT_TOKENS or "
                f"reduce GEMINI_THINKING_BUDGET."
            )
        content = candidate.get("content", {})
        parts = content.get("parts")
        if not parts:
            usage = data.get("usageMetadata", {})
            raise LLMClientError(
                f"Gemini returned empty content (finishReason={finish_reason}, usage={usage})."
            )
        text = parts[0].get("text", "")
        if not text:
            usage = data.get("usageMetadata", {})
            raise LLMClientError(
                f"Gemini returned empty text (finishReason={finish_reason}, usage={usage})."
            )
        usage = data.get("usageMetadata", {})
        prompt_tokens = int(usage.get("promptTokenCount", 0))
        output_tokens = int(usage.get("candidatesTokenCount", 0))
        return text, prompt_tokens, output_tokens

    def translate_http_error(self, exc: httpx.HTTPStatusError) -> LLMClientError:
        if exc.response is None:
            return LLMClientError("Gemini API request failed.")
        status_code = exc.response.status_code
        if status_code == 400:
            return LLMClientError("Gemini API rejected the request.")
        if status_code in (401, 403):
            return LLMClientError("Gemini API authentication failed.")
        if status_code == 429:
            return LLMClientError(
                "Gemini API rate limit reached. Try again shortly.", status_code=503
            )
        return LLMClientError("Gemini API request failed.")

    async def close(self):
        await self.http_client.aclose()
