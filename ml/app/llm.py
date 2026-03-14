import json
import re
import logging
import httpx
from typing import Any, Dict

from app.budget import BudgetTracker, DailyBudgetExceededError, format_usd_from_nanodollars
from app.config import MAX_OUTPUT_TOKENS
from app.prompts import SYSTEM_PROMPT_GENERATE, SYSTEM_PROMPT_EDIT
from app.validator import validate_bpmn_xml
from app.bpmn_fix import ensure_incoming_outgoing, strip_bpmn_diagram

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class LLMClientError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class LLMClient:
    def __init__(
        self,
        api_key: str,
        budget_tracker: BudgetTracker,
        model: str = "gemini-3.1-flash-lite-preview",
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ):
        self.api_key = api_key
        self.model = model
        self.budget_tracker = budget_tracker
        self.max_output_tokens = max_output_tokens
        self.http_client = httpx.AsyncClient(
            timeout=120.0,
            headers={"x-goog-api-key": self.api_key},
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
            },
        }

    async def _count_tokens(self, payload: Dict[str, Any]) -> int:
        response = await self.http_client.post(
            f"{GEMINI_API_URL}/{self.model}:countTokens",
            json={"generateContentRequest": payload},
        )
        response.raise_for_status()
        data = response.json()
        total_tokens = data.get("totalTokens")
        if total_tokens is None:
            raise LLMClientError("Gemini API did not return token counts.")
        return int(total_tokens)

    def _translate_http_error(self, exc: httpx.HTTPStatusError) -> LLMClientError:
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

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        payload = self._build_payload(system_prompt, user_prompt)
        reservation = None

        try:
            prompt_tokens = await self._count_tokens(payload)
            reservation = self.budget_tracker.reserve_for_call(prompt_tokens)
            response = await self.http_client.post(
                f"{GEMINI_API_URL}/{self.model}:generateContent",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except DailyBudgetExceededError:
            raise
        except httpx.HTTPStatusError as exc:
            if reservation is not None:
                self.budget_tracker.release_reservation(reservation)
            raise self._translate_http_error(exc) from exc
        except httpx.HTTPError as exc:
            if reservation is not None:
                self.budget_tracker.release_reservation(reservation)
            raise LLMClientError("Unable to reach Gemini API.") from exc
        except Exception:
            if reservation is not None:
                self.budget_tracker.release_reservation(reservation)
            raise

        usage_metadata = data.get("usageMetadata", {})
        actual_prompt_tokens = int(usage_metadata.get("promptTokenCount", prompt_tokens))
        actual_output_tokens = int(usage_metadata.get("candidatesTokenCount", 0))
        actual_cost = self.budget_tracker.finalize_call(
            reservation=reservation,
            prompt_tokens=actual_prompt_tokens,
            output_tokens=actual_output_tokens,
        )
        logger.info(
            "Gemini request completed: %s prompt tokens, %s output tokens, $%s actual cost",
            actual_prompt_tokens,
            actual_output_tokens,
            format_usd_from_nanodollars(actual_cost),
        )

        if "candidates" not in data or not data["candidates"]:
            raise LLMClientError("Gemini API returned no completion.")

        text = data["candidates"][0]["content"]["parts"][0]["text"]
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
        for attempt in range(max_retries):
            raw = await self._call_gemini(SYSTEM_PROMPT_GENERATE, user_prompt)
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
        for attempt in range(max_retries):
            raw = await self._call_gemini(SYSTEM_PROMPT_EDIT, user_prompt)
            result = self._extract_json(raw)

            if "bpmn_xml" not in result:
                raise ValueError("LLM response missing 'bpmn_xml' field")

            new_xml = self._unescape_xml(result["bpmn_xml"])
            new_xml = strip_bpmn_diagram(new_xml)
            error = validate_bpmn_xml(new_xml)
            if error is None:
                new_xml = ensure_incoming_outgoing(new_xml)
                return {"bpmn_xml": new_xml}

            logger.warning(f"Attempt {attempt + 1}: Invalid edited BPMN XML: {error}")
            user_prompt = (
                f"Your previous edit produced invalid BPMN XML. Error: {error}\n\n"
                f"Original BPMN XML:\n```xml\n{bpmn_xml}\n```\n\n"
                f"Modification instruction: {prompt}\n\n"
                f"Please fix the issue and try again."
            )

        raise ValueError(f"Failed to produce valid edited BPMN XML after {max_retries} attempts. Last error: {error}")

    async def close(self):
        await self.http_client.aclose()
