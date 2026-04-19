import json
import re
import logging
import httpx
from typing import Any, Dict

from app.budget import BudgetTracker, DailyBudgetExceededError, format_usd_from_nanodollars
from app.config import MAX_OUTPUT_TOKENS
from app.prompts import SYSTEM_PROMPT_CLASSIFY, SYSTEM_PROMPT_GENERATE, SYSTEM_PROMPT_EDIT
from app.validator import validate_bpmn_xml, get_bpmn_warnings
from app.bpmn_fix import ensure_incoming_outgoing, ensure_lane_refs, strip_bpmn_diagram

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMClientError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class GeminiBackend:
    """Direct Google Gemini API backend."""

    def __init__(self, api_key: str, model: str, max_output_tokens: int):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.http_client = httpx.AsyncClient(
            timeout=60.0,
            headers={"x-goog-api-key": api_key},
        )

    def _build_payload(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": self.max_output_tokens,
                "responseMimeType": "application/json",
                # Disable Gemini 2.5 "thinking" tokens — they consume the
                # output budget and silently truncate long BPMN XML.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }

    async def count_tokens(self, system_prompt: str, user_prompt: str) -> int:
        payload = self._build_payload(system_prompt, user_prompt)
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

    async def generate(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        """Returns (text, prompt_tokens, output_tokens)."""
        payload = self._build_payload(system_prompt, user_prompt)
        response = await self.http_client.post(
            f"{GEMINI_API_URL}/{self.model}:generateContent",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        if "candidates" not in data or not data["candidates"]:
            raise LLMClientError("Gemini API returned no completion.")

        text = data["candidates"][0]["content"]["parts"][0]["text"]
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
            return LLMClientError("Gemini API rate limit reached. Try again shortly.", status_code=503)
        return LLMClientError("Gemini API request failed.")

    async def close(self):
        await self.http_client.aclose()


class PolzaBackend:
    """OpenAI-compatible backend via polza.ai."""

    def __init__(self, api_key: str, model: str, base_url: str, max_output_tokens: int):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.http_client = httpx.AsyncClient(
            timeout=60.0,
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def count_tokens(self, system_prompt: str, user_prompt: str) -> int:
        # Polza doesn't have a separate count endpoint; estimate from char count
        # ~4 chars per token is a rough estimate
        total_chars = len(system_prompt) + len(user_prompt)
        return total_chars // 4

    async def generate(self, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        """Returns (text, prompt_tokens, output_tokens)."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
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
        if status_code == 400:
            return LLMClientError("API rejected the request.")
        if status_code in (401, 403):
            return LLMClientError("API authentication failed.")
        if status_code == 402:
            return LLMClientError(
                "Polza API credits exhausted. Top up your balance at "
                "https://polza.ai/dashboard or switch LLM_BACKEND to 'gemini'.",
                status_code=402,
            )
        if status_code == 429:
            return LLMClientError("API rate limit reached. Try again shortly.", status_code=503)
        return LLMClientError("API request failed.")

    async def close(self):
        await self.http_client.aclose()


class LLMClient:
    def __init__(
        self,
        budget_tracker: BudgetTracker,
        backend: GeminiBackend | PolzaBackend,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ):
        self.backend = backend
        self.budget_tracker = budget_tracker
        self.max_output_tokens = max_output_tokens

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        reservation = None

        try:
            prompt_tokens = await self.backend.count_tokens(system_prompt, user_prompt)
            reservation = self.budget_tracker.reserve_for_call(prompt_tokens)
            text, actual_prompt_tokens, actual_output_tokens = await self.backend.generate(
                system_prompt, user_prompt
            )
        except DailyBudgetExceededError:
            raise
        except httpx.HTTPStatusError as exc:
            if reservation is not None:
                self.budget_tracker.release_reservation(reservation)
            raise self.backend.translate_http_error(exc) from exc
        except httpx.HTTPError as exc:
            if reservation is not None:
                self.budget_tracker.release_reservation(reservation)
            raise LLMClientError("Unable to reach LLM API.") from exc
        except Exception:
            if reservation is not None:
                self.budget_tracker.release_reservation(reservation)
            raise

        if not actual_prompt_tokens:
            actual_prompt_tokens = prompt_tokens

        actual_cost = self.budget_tracker.finalize_call(
            reservation=reservation,
            prompt_tokens=actual_prompt_tokens,
            output_tokens=actual_output_tokens,
        )
        logger.info(
            "LLM request completed: %s prompt tokens, %s output tokens, $%s actual cost",
            actual_prompt_tokens,
            actual_output_tokens,
            format_usd_from_nanodollars(actual_cost),
        )

        return text

    def _extract_json(self, text: str) -> dict:
        text = text.strip()
        json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            raise ValueError(f"Could not parse JSON from LLM response: {text[:500]}")

    def _unescape_xml(self, xml_str: str) -> str:
        if not xml_str.strip().startswith("<"):
            try:
                xml_str = json.loads(f'"{xml_str}"')
            except Exception:
                pass
        return xml_str

    async def generate(self, description: str) -> dict:
        logger.info("Calling LLM for BPMN generation")
        user_prompt = f"Generate a BPMN 2.0 diagram for the following business process:\n\n{description}"

        max_retries = 3
        error = None
        for attempt in range(max_retries):
            raw = await self._call_llm(SYSTEM_PROMPT_GENERATE, user_prompt)
            result = self._extract_json(raw)

            if "bpmn_xml" not in result:
                raise ValueError("LLM response missing 'bpmn_xml' field")
            if "session_name" not in result:
                raise ValueError("LLM response missing 'session_name' field")

            bpmn_xml = self._unescape_xml(result["bpmn_xml"])
            bpmn_xml = strip_bpmn_diagram(bpmn_xml)
            error = validate_bpmn_xml(bpmn_xml)
            if error is None:
                bpmn_xml = ensure_incoming_outgoing(bpmn_xml)
                bpmn_xml = ensure_lane_refs(bpmn_xml)
                for w in get_bpmn_warnings(bpmn_xml):
                    logger.warning("BPMN warning (generate): %s", w)
                return {"bpmn_xml": bpmn_xml, "session_name": result["session_name"]}

            logger.warning(f"Attempt {attempt + 1}: Invalid BPMN XML: {error}")
            user_prompt = (
                f"Your previous response produced invalid BPMN XML. Error: {error}\n\n"
                f"Please fix the issue and regenerate. Original request:\n"
                f"Generate a BPMN 2.0 diagram for the following business process:\n\n{description}"
            )

        raise ValueError(f"Failed to generate valid BPMN XML after {max_retries} attempts. Last error: {error}")

    async def edit(self, prompt: str, bpmn_xml: str) -> dict:
        logger.info("Calling LLM for BPMN edit")
        user_prompt = (
            f"Current BPMN XML:\n```xml\n{bpmn_xml}\n```\n\n"
            f"Modification instruction: {prompt}"
        )

        max_retries = 3
        error = None
        for attempt in range(max_retries):
            raw = await self._call_llm(SYSTEM_PROMPT_EDIT, user_prompt)
            result = self._extract_json(raw)

            if "bpmn_xml" not in result:
                raise ValueError("LLM response missing 'bpmn_xml' field")

            new_xml = self._unescape_xml(result["bpmn_xml"])
            new_xml = strip_bpmn_diagram(new_xml)
            error = validate_bpmn_xml(new_xml)
            if error is None:
                new_xml = ensure_incoming_outgoing(new_xml)
                new_xml = ensure_lane_refs(new_xml)
                for w in get_bpmn_warnings(new_xml):
                    logger.warning("BPMN warning (edit): %s", w)
                return {"bpmn_xml": new_xml}

            logger.warning(f"Attempt {attempt + 1}: Invalid edited BPMN XML: {error}")
            user_prompt = (
                f"Your previous edit produced invalid BPMN XML. Error: {error}\n\n"
                f"Original BPMN XML:\n```xml\n{bpmn_xml}\n```\n\n"
                f"Modification instruction: {prompt}\n\n"
                f"Please fix the issue and try again."
            )

        raise ValueError(f"Failed to produce valid edited BPMN XML after {max_retries} attempts. Last error: {error}")

    async def classify(self, text: str) -> dict:
        """Classify whether input is a valid BPMN request."""
        logger.info("Calling LLM for input classification")
        raw = await self._call_llm(SYSTEM_PROMPT_CLASSIFY, text)
        result = self._extract_json(raw)
        return {
            "is_valid": bool(result.get("is_valid", False)),
            "reason": result.get("reason", ""),
        }

    async def close(self):
        await self.backend.close()
