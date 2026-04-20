import json
import re
import logging
import httpx
from typing import Any, Dict, Optional

from app.budget import BudgetTracker, DailyBudgetExceededError, format_usd_from_nanodollars
from app.config import GEMINI_THINKING_BUDGET, MAX_OUTPUT_TOKENS
from app.prompts import SYSTEM_PROMPT_CLASSIFY, SYSTEM_PROMPT_GENERATE, SYSTEM_PROMPT_EDIT
from app.validator import validate_bpmn_xml, get_bpmn_warnings
from app.bpmn_fix import (
    ensure_incoming_outgoing,
    ensure_lane_refs,
    fix_missing_namespace_declarations,
    strip_bpmn_diagram,
)

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"


# Response schemas used to force strict JSON structure via Gemini's
# `responseJsonSchema` (see Gemini structured-output docs). Passing
# responseMimeType=application/json *without* a schema was observed to
# produce double-escaped payloads on long XML outputs — Gemini would wrap
# the whole response in an extra layer of string quoting, breaking parse.
# With an explicit schema the model is constrained to emit a real object.
GENERATE_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "bpmn_xml": {
            "type": "string",
            "description": "The complete BPMN 2.0 XML document.",
        },
        "session_name": {
            "type": "string",
            "description": "Short 3-5 word process name in the user's input language.",
        },
    },
    "required": ["bpmn_xml", "session_name"],
    "propertyOrdering": ["bpmn_xml", "session_name"],
}

EDIT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "bpmn_xml": {
            "type": "string",
            "description": "The updated BPMN 2.0 XML document.",
        },
    },
    "required": ["bpmn_xml"],
}

CLASSIFY_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_valid": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_valid"],
}


# Trigger phrases that force a `<bpmn:laneSet>` in the generated diagram.
# If any of these patterns appear in the user's description, the output
# MUST contain a laneSet or we retry. See generate() — this is a belt-and-
# suspenders check on top of the prompt instruction. Keep patterns tight
# so we don't false-positive on casual mentions.
_EXPLICIT_ROLE_PATTERNS = [
    re.compile(r"\bисполь[зс]уй\s+рол[иея]", re.IGNORECASE),
    re.compile(r"\bс\s+рол[яеия][хм]?\b", re.IGNORECASE),
    re.compile(r"\bв\s+\d+\s+рол[яеия][хм]?\b", re.IGNORECASE),
    re.compile(r"\bрол[иея]\s*:", re.IGNORECASE),
    re.compile(r"\bучастник[иа]?\s*:", re.IGNORECASE),
    re.compile(r"\bактёр[ыа]?\s*:", re.IGNORECASE),
    re.compile(r"\buse\s+roles?\b", re.IGNORECASE),
    re.compile(r"\bwith\s+roles?\b", re.IGNORECASE),
    re.compile(r"\bin\s+\d+\s+roles?\b", re.IGNORECASE),
    re.compile(r"\broles?\s*:", re.IGNORECASE),
    re.compile(r"\bactors?\s*:", re.IGNORECASE),
    re.compile(r"\bparticipants?\s*:", re.IGNORECASE),
    re.compile(r"\bswimlanes?\s*:", re.IGNORECASE),
    re.compile(r"\bby\s+role\b", re.IGNORECASE),
]


def description_requires_lanes(description: str) -> bool:
    """True if the description contains an explicit role-enumeration hint.

    Used as a post-generation guard: if the user clearly asked for roles
    but the LLM produced a flat process (no laneSet), we retry with an
    explicit correction prompt instead of silently shipping a degraded
    diagram.
    """
    for pat in _EXPLICIT_ROLE_PATTERNS:
        if pat.search(description):
            return True
    return False


_LANESET_MARKER = re.compile(r"<\s*(?:bpmn:)?laneSet\b", re.IGNORECASE)


def xml_has_lanes(xml: str) -> bool:
    """True if the BPMN XML contains a non-empty `<bpmn:laneSet>`."""
    return bool(_LANESET_MARKER.search(xml))


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
        # 180s lets a complex request with thinkingBudget=2048 + long
        # BPMN output complete without HTTP-level timeouts.
        self.http_client = httpx.AsyncClient(
            timeout=180.0,
            headers={"x-goog-api-key": api_key},
        )

    def _build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        generation_config: Dict[str, Any] = {
            "temperature": 0.2,
            "maxOutputTokens": self.max_output_tokens,
            "responseMimeType": "application/json",
            # Thinking budget — 0 off / -1 dynamic / >0 cap. See
            # config.GEMINI_THINKING_BUDGET. Ignored by non-thinking models.
            "thinkingConfig": {"thinkingBudget": GEMINI_THINKING_BUDGET},
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
            "systemInstruction": {
                "parts": [{"text": system_prompt}]
            },
            "generationConfig": generation_config,
        }

    async def count_tokens(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict[str, Any]] = None,
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
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, int, int]:
        """Returns (text, prompt_tokens, output_tokens)."""
        payload = self._build_payload(system_prompt, user_prompt, response_schema)
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
                f"Gemini returned empty content (finishReason={finish_reason}, "
                f"usage={usage})."
            )
        text = parts[0].get("text", "")
        if not text:
            usage = data.get("usageMetadata", {})
            raise LLMClientError(
                f"Gemini returned empty text (finishReason={finish_reason}, "
                f"usage={usage})."
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
            return LLMClientError("Gemini API rate limit reached. Try again shortly.", status_code=503)
        return LLMClientError("Gemini API request failed.")

    async def close(self):
        await self.http_client.aclose()


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
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> int:
        # Polza doesn't have a separate count endpoint; estimate from char count
        # ~4 chars per token is a rough estimate
        total_chars = len(system_prompt) + len(user_prompt)
        return total_chars // 4

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, int, int]:
        """Returns (text, prompt_tokens, output_tokens)."""
        # OpenAI-compatible strict-schema mode via response_format=json_schema.
        # Polza proxies to various upstream models — most OpenAI-compatible
        # endpoints accept this shape. Falls back to plain json_object when
        # no schema is provided.
        if response_schema is not None:
            response_format: Dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "bpmn_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_output_tokens,
            "response_format": response_format,
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

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: Optional[Dict[str, Any]] = None,
    ) -> str:
        reservation = None

        try:
            prompt_tokens = await self.backend.count_tokens(
                system_prompt, user_prompt, response_schema
            )
            reservation = self.budget_tracker.reserve_for_call(prompt_tokens)
            text, actual_prompt_tokens, actual_output_tokens = await self.backend.generate(
                system_prompt, user_prompt, response_schema
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

    def _repair_double_escaped_json(self, text: str) -> str | None:
        """Gemini flash-lite sometimes emits JSON where the entire payload is
        double-escaped: every `"` inside the JSON structure becomes `\\"`.

        Example malformed:
            {"bpmn_xml": "<?xml...\\" version=\\"1.0\\"...</definitions>\\", \\"session_name\\": \\"…\\"}

        The outer structure has `\\",` where it should have `",`. Reverting
        one level of escaping turns this into valid JSON.

        Returns the repaired text, or None if no repair is possible.
        """
        # Heuristic: if the end contains `\"}` (escaped quote before closing
        # brace) but the JSON couldn't parse, assume double-escaping.
        if r'\"}' not in text and r'\",' not in text:
            return None
        # Decode one level: replace `\"` → `"`, `\\` → `\`, `\n` → real newline.
        # We do this via json.loads wrapping — safest way.
        try:
            # Wrap in quotes and use json.loads to unescape exactly one level.
            inner = json.loads(f'"{text}"')
            return inner
        except json.JSONDecodeError:
            # Fallback: manual unescape of common patterns.
            return (
                text.replace(r'\"', '"')
                    .replace(r'\\', '\\')
                    .replace(r'\n', '\n')
                    .replace(r'\t', '\t')
            )

    def _repair_structural_escape(self, text: str) -> str | None:
        """Recover Gemini flash-lite's "structural escape" pathology.

        Seen on long-role prompts: model emits a TOP-LEVEL `{...}` but
        escapes the *structural* quotes between fields, then appends a
        stray trailing `"`. Example (compact):

            {"bpmn_xml": "<XML>\\", \\"session_name\\": \\"foo\\"}"

        i.e. what should be `", "session_name": "foo"}` comes out as
        `\\", \\"session_name\\": \\"foo\\"}"`. Normal parse fails because
        the outer `{` is never closed — the string value swallows the
        rest of the object.

        Returns the repaired text or None if heuristics don't match.
        """
        if not text.startswith("{"):
            return None
        # Pathology marker: structural comma-quote-key pattern escaped.
        if r'\", \"' not in text and r'\",\"' not in text:
            return None
        candidate = text
        # Strip a single trailing stray `"` (the outer bookend).
        if candidate.endswith('"') and not candidate.endswith(r'\"'):
            candidate = candidate[:-1]
        # Revert one level of escape on structural tokens only:
        #   \",   → ",    (end of string value, start of next field)
        #   \":   → ":    (key-value separator)
        #   \"}   → "}    (end of last string value)
        #   , \"  → , "   (start of next string value after comma)
        #   : \"  → : "   (start of string value after colon)
        repaired = (
            candidate
            .replace(r'\", \"', '", "')
            .replace(r'\",\"', '","')
            .replace(r'\": \"', '": "')
            .replace(r'\":\"', '":"')
            .replace(r'\"}', '"}')
            .replace(r': \"', ': "')
            .replace(r':\"', ':"')
        )
        return repaired

    def _extract_json(self, text: str) -> dict:
        text = text.strip()
        json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        try:
            parsed = json.loads(text)
            # Recovery path 0: if Gemini stringified the entire response
            # (whole thing is a JSON string whose content is JSON), unwrap
            # exactly one level. Observed rarely on free-form responses.
            if isinstance(parsed, str):
                try:
                    reparsed = json.loads(parsed)
                    if isinstance(reparsed, dict):
                        logger.info(
                            "Unwrapped stringified JSON from LLM response."
                        )
                        return reparsed
                except json.JSONDecodeError:
                    pass
                raise ValueError(
                    f"LLM returned a plain string, not a JSON object "
                    f"(len={len(text)})"
                )
            return parsed
        except json.JSONDecodeError as first_err:
            # Recovery path 1: Gemini sometimes double-escapes the entire JSON.
            repaired = self._repair_double_escaped_json(text)
            if repaired is not None:
                try:
                    result = json.loads(repaired)
                    logger.info("Recovered double-escaped JSON from LLM response.")
                    return result
                except json.JSONDecodeError:
                    pass  # fall through
            # Recovery path 2: structural-escape pathology — top-level `{`
            # with INNER structural quotes escaped (flash-lite on long
            # role-rich prompts, observed with Anton's "командировка" spec).
            struct_repaired = self._repair_structural_escape(text)
            if struct_repaired is not None:
                try:
                    result = json.loads(struct_repaired)
                    logger.info(
                        "Recovered structurally-escaped JSON from LLM response."
                    )
                    return result
                except json.JSONDecodeError:
                    pass  # fall through
            # Recovery path 3: extract outermost {...} via regex (handles
            # chatty wrappers like "Here's your JSON: {...}").
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(0))
                except json.JSONDecodeError as third_err:
                    logger.error(
                        "JSON parse failed (incl. recovery). len=%d first_err=%s "
                        "third_err=%s head=%r tail=%r",
                        len(text), first_err, third_err, text[:200], text[-200:],
                    )
                    raise ValueError(
                        f"Could not parse JSON from LLM response "
                        f"(len={len(text)}, err={third_err})"
                    )
            logger.error(
                "JSON parse failed, no outer braces. len=%d err=%s head=%r tail=%r",
                len(text), first_err, text[:200], text[-200:],
            )
            raise ValueError(
                f"Could not parse JSON from LLM response (len={len(text)})"
            )

    def _unescape_xml(self, xml_str: str) -> str:
        if not xml_str.strip().startswith("<"):
            try:
                xml_str = json.loads(f'"{xml_str}"')
            except Exception:
                pass
        return xml_str

    async def generate(self, description: str) -> dict:
        logger.info("Calling LLM for BPMN generation")
        original_prompt = (
            f"Generate a BPMN 2.0 diagram for the following business "
            f"process:\n\n{description}"
        )
        user_prompt = original_prompt
        needs_lanes = description_requires_lanes(description)
        if needs_lanes:
            logger.info(
                "Description contains explicit role-enumeration hint — "
                "lanes are required in output."
            )

        max_retries = 3
        last_xml_without_lanes: Optional[str] = None
        last_session_name: str = ""
        error: Optional[str] = None
        for attempt in range(max_retries):
            raw = await self._call_llm(
                SYSTEM_PROMPT_GENERATE, user_prompt, GENERATE_RESPONSE_SCHEMA
            )
            try:
                result = self._extract_json(raw)
            except ValueError as exc:
                # JSON parse failure (incl. double-escape, truncation).
                # Retry with a cleaner prompt rather than 500-ing immediately.
                error = f"JSON parse error: {exc}"
                logger.warning("Attempt %d: %s", attempt + 1, error)
                user_prompt = original_prompt  # fresh request, no history
                continue

            if "bpmn_xml" not in result:
                error = "LLM response missing 'bpmn_xml' field"
                logger.warning("Attempt %d: %s", attempt + 1, error)
                user_prompt = original_prompt
                continue
            if "session_name" not in result:
                error = "LLM response missing 'session_name' field"
                logger.warning("Attempt %d: %s", attempt + 1, error)
                user_prompt = original_prompt
                continue

            bpmn_xml = self._unescape_xml(result["bpmn_xml"])
            bpmn_xml = strip_bpmn_diagram(bpmn_xml)
            bpmn_xml = fix_missing_namespace_declarations(bpmn_xml)
            error = validate_bpmn_xml(bpmn_xml)
            if error is None:
                # Post-gen lane guard: user explicitly asked for roles but
                # the model produced a flat process — retry with a hard
                # correction prompt rather than silently shipping a
                # degraded diagram. Cap at one such retry (the last
                # attempt) so we don't loop forever on a model that
                # genuinely can't extract lanes.
                if (
                    needs_lanes
                    and not xml_has_lanes(bpmn_xml)
                    and attempt < max_retries - 1
                ):
                    last_xml_without_lanes = bpmn_xml
                    last_session_name = result["session_name"]
                    logger.warning(
                        "Attempt %d: description requested roles but "
                        "output has no <laneSet>. Retrying with explicit "
                        "lane correction prompt.",
                        attempt + 1,
                    )
                    user_prompt = (
                        f"Your previous response ignored the explicit role "
                        f"requirement. The user's description lists SEPARATE "
                        f"ROLES — you MUST produce a `<bpmn:laneSet>` with "
                        f"one `<bpmn:lane>` per role, and reference every "
                        f"flow node in exactly one `<bpmn:flowNodeRef>`. See "
                        f"SECTION 4 of your instructions.\n\n"
                        f"Original request:\n{original_prompt}"
                    )
                    error = "output missing required <laneSet>"
                    continue

                bpmn_xml = ensure_incoming_outgoing(bpmn_xml)
                bpmn_xml = ensure_lane_refs(bpmn_xml)
                for w in get_bpmn_warnings(bpmn_xml):
                    logger.warning("BPMN warning (generate): %s", w)
                return {"bpmn_xml": bpmn_xml, "session_name": result["session_name"]}

            logger.warning("Attempt %d: Invalid BPMN XML: %s", attempt + 1, error)
            user_prompt = (
                f"Your previous response produced invalid BPMN XML. "
                f"Error: {error}\n\nPlease fix the issue and regenerate. "
                f"Original request:\n{original_prompt}"
            )

        # Fallback: if we consumed all retries chasing lanes but every
        # attempt was structurally valid just lane-less, ship the last
        # valid lane-less XML rather than failing the whole request.
        if last_xml_without_lanes is not None:
            logger.warning(
                "All %d attempts produced lane-less XML despite explicit "
                "role hint; returning last valid structure anyway.",
                max_retries,
            )
            bpmn_xml = ensure_incoming_outgoing(last_xml_without_lanes)
            bpmn_xml = ensure_lane_refs(bpmn_xml)
            return {"bpmn_xml": bpmn_xml, "session_name": last_session_name}

        raise ValueError(
            f"Failed to generate valid BPMN XML after {max_retries} attempts. "
            f"Last error: {error}"
        )

    async def edit(self, prompt: str, bpmn_xml: str) -> dict:
        logger.info("Calling LLM for BPMN edit")
        original_prompt = (
            f"Current BPMN XML:\n```xml\n{bpmn_xml}\n```\n\n"
            f"Modification instruction: {prompt}"
        )
        user_prompt = original_prompt

        max_retries = 3
        error: Optional[str] = None
        for attempt in range(max_retries):
            raw = await self._call_llm(
                SYSTEM_PROMPT_EDIT, user_prompt, EDIT_RESPONSE_SCHEMA
            )
            try:
                result = self._extract_json(raw)
            except ValueError as exc:
                error = f"JSON parse error: {exc}"
                logger.warning("Attempt %d: %s", attempt + 1, error)
                user_prompt = original_prompt
                continue

            if "bpmn_xml" not in result:
                error = "LLM response missing 'bpmn_xml' field"
                logger.warning("Attempt %d: %s", attempt + 1, error)
                user_prompt = original_prompt
                continue

            new_xml = self._unescape_xml(result["bpmn_xml"])
            new_xml = strip_bpmn_diagram(new_xml)
            new_xml = fix_missing_namespace_declarations(new_xml)
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
        raw = await self._call_llm(
            SYSTEM_PROMPT_CLASSIFY, text, CLASSIFY_RESPONSE_SCHEMA
        )
        result = self._extract_json(raw)
        return {
            "is_valid": bool(result.get("is_valid", False)),
            "reason": result.get("reason", ""),
        }

    async def close(self):
        await self.backend.close()
