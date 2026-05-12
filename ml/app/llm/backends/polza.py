"""OpenAI-compatible backend via polza.ai."""

from typing import Any

import httpx

from app.config import GEMINI_THINKING_BUDGET
from app.llm.errors import LLMClientError


def _map_budget_to_effort(budget: int) -> str | None:
    """Map Gemini-native `thinkingBudget` (token count) to OpenAI-style
    `reasoning.effort` enum used by Polza.

    Polza's OpenAI-compatible surface doesn't accept raw token counts —
    only low/medium/high. Keep thresholds aligned with what we've
    benchmarked on Gemini direct: ≤ 2048 ≈ low, 2049-5000 ≈ medium,
    > 5000 ≈ high. Budget = 0 → no reasoning.
    """
    if budget <= 0:
        return None
    if budget <= 2048:
        return "low"
    if budget <= 5000:
        return "medium"
    return "high"


class PolzaBackend:
    """OpenAI-compatible backend via polza.ai."""

    def __init__(self, api_key: str, model: str, base_url: str, max_output_tokens: int):
        self.model = model
        self.max_output_tokens = max_output_tokens
        # 180s matches GeminiBackend — gives thinking + long XML output
        # room to complete without HTTP timeout.
        self.http_client = httpx.AsyncClient(
            timeout=180.0,
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def count_tokens(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> int:
        # Polza doesn't have a separate count endpoint; estimate from char count
        # ~4 chars per token is a rough estimate
        total_chars = len(system_prompt) + len(user_prompt)
        return total_chars // 4

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> tuple[str, int, int]:
        """Returns (text, prompt_tokens, output_tokens)."""
        # OpenAI-compatible strict-schema mode via response_format=json_schema.
        # Polza proxies to various upstream models — most OpenAI-compatible
        # endpoints accept this shape. Falls back to plain json_object when
        # no schema is provided.
        if response_schema is not None:
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "bpmn_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_output_tokens,
            "response_format": response_format,
        }
        # Polza supports OpenAI-style `reasoning.effort` (o-series shape).
        # We mirror Gemini's thinkingBudget → effort bucket so both
        # backends produce comparable branching quality on long specs.
        # Verified experimentally: `reasoning: {effort: "high"}` is
        # the form Polza accepts; `reasoning_effort` (flat) and
        # `thinking: {budget_tokens: ...}` are silently ignored.
        effort = _map_budget_to_effort(GEMINI_THINKING_BUDGET)
        if effort is not None:
            payload["reasoning"] = {"effort": effort}
        response = await self.http_client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            raise LLMClientError("Polza API returned no completion.")

        text = choices[0]["message"]["content"]
        usage = data.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        return text, prompt_tokens, output_tokens

    def translate_http_error(self, exc: httpx.HTTPStatusError) -> LLMClientError:
        if exc.response is None:
            return LLMClientError("Polza API request failed.")
        status_code = exc.response.status_code
        # Polza returns a structured error body — parse it so we can
        # distinguish "balance=0" from "daily cap reached" (both 402).
        polza_code = ""
        polza_message = ""
        try:
            body = exc.response.json()
            err = body.get("error", {})
            polza_code = err.get("code", "")
            polza_message = err.get("message", "")
        except Exception:
            pass

        if status_code == 400:
            return LLMClientError("Polza API rejected the request.")
        if status_code in (401, 403):
            return LLMClientError("Polza API authentication failed.")
        if status_code == 402:
            # The Polza dashboard has TWO separate guards, both returning
            # 402 with code=INSUFFICIENT_BALANCE but different messages:
            #   * "Достигнут дневной лимит по сумме" — daily spend cap
            #     (balance may still be positive, just raise the cap).
            #   * generic balance depletion — top up the account.
            # Surface Polza's own wording so operators know which knob
            # to reach for.
            prefix = "Polza API error"
            if polza_code:
                prefix = f"Polza API error ({polza_code})"
            if "дневн" in polza_message.lower() or "daily" in polza_message.lower():
                hint = (
                    "Daily spend cap reached on Polza — raise it at "
                    "https://polza.ai/dashboard or wait until the cap "
                    "resets. As a quick workaround, flip "
                    "LLM_BACKEND=gemini on the server."
                )
            else:
                hint = (
                    "Top up at https://polza.ai/dashboard or flip "
                    "LLM_BACKEND=gemini on the server."
                )
            detail = f"{prefix}: {polza_message}. {hint}" if polza_message else f"{prefix}. {hint}"
            return LLMClientError(detail, status_code=402)
        if status_code == 429:
            return LLMClientError(
                "Polza API rate limit reached. Try again shortly.", status_code=503
            )
        return LLMClientError("Polza API request failed.")

    async def close(self):
        await self.http_client.aclose()
