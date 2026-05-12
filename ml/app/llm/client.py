"""High-level `LLMClient` — orchestrates budget reservation, backend
call, response parsing/repair, BPMN validation, and retry policy.

The instance methods `_extract_json`, `_repair_*`, `_unescape_xml`
delegate to module-level functions in `app.llm.json_recovery`. They're
kept as instance methods so existing tests (`client._extract_json(...)`)
keep working — see `test_json_recovery.py`.
"""

from typing import Any

import httpx
import structlog

from app.bpmn_fix import (
    ensure_incoming_outgoing,
    ensure_lane_refs,
    fix_missing_namespace_declarations,
    strip_bpmn_diagram,
)
from app.bpmn_layout import layout_bpmn
from app.budget import BudgetTracker, DailyBudgetExceededError, format_usd_from_nanodollars
from app.config import MAX_OUTPUT_TOKENS
from app.llm.backends import GeminiBackend, PolzaBackend
from app.llm.errors import LLMClientError
from app.llm.json_recovery import (
    extract_json,
    repair_double_escaped_json,
    repair_structural_escape,
    unescape_xml,
)
from app.llm.lane_guard import description_requires_lanes, xml_has_lanes
from app.llm.schemas import (
    CLASSIFY_RESPONSE_SCHEMA,
    EDIT_RESPONSE_SCHEMA,
    GENERATE_RESPONSE_SCHEMA,
)
from app.prompts import SYSTEM_PROMPT_CLASSIFY, SYSTEM_PROMPT_EDIT, SYSTEM_PROMPT_GENERATE
from app.validator import get_bpmn_warnings, validate_bpmn_xml

logger = structlog.get_logger(__name__)


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

    # -- JSON recovery shims -------------------------------------------------
    # Kept as instance methods so test_json_recovery.py keeps working.

    def _repair_double_escaped_json(self, text: str) -> str | None:
        return repair_double_escaped_json(text)

    def _repair_structural_escape(self, text: str) -> str | None:
        return repair_structural_escape(text)

    def _extract_json(self, text: str) -> dict:
        return extract_json(text)

    def _unescape_xml(self, xml_str: str) -> str:
        return unescape_xml(xml_str)

    # -- Internal: one backend call wrapped in budget reservation ----------

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any] | None = None,
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

    # -- Public API ---------------------------------------------------------

    async def generate(self, description: str) -> dict:
        logger.info("Calling LLM for BPMN generation")
        original_prompt = (
            f"Generate a BPMN 2.0 diagram for the following business process:\n\n{description}"
        )
        user_prompt = original_prompt
        needs_lanes = description_requires_lanes(description)
        if needs_lanes:
            logger.info(
                "Description contains explicit role-enumeration hint — "
                "lanes are required in output."
            )

        max_retries = 3
        last_xml_without_lanes: str | None = None
        last_session_name: str = ""
        error: str | None = None
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
                # degraded diagram.
                if needs_lanes and not xml_has_lanes(bpmn_xml) and attempt < max_retries - 1:
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
                # Server-side lane-aware layout: writes BPMNDiagram with
                # lane shapes + flow node positions + edge waypoints.
                bpmn_xml = layout_bpmn(bpmn_xml)
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
            bpmn_xml = layout_bpmn(bpmn_xml)
            return {"bpmn_xml": bpmn_xml, "session_name": last_session_name}

        raise ValueError(
            f"Failed to generate valid BPMN XML after {max_retries} attempts. "
            f"Last error: {error}"
        )

    async def edit(self, prompt: str, bpmn_xml: str) -> dict:
        logger.info("Calling LLM for BPMN edit")
        original_prompt = (
            f"Current BPMN XML:\n```xml\n{bpmn_xml}\n```\n\nModification instruction: {prompt}"
        )
        user_prompt = original_prompt

        max_retries = 3
        error: str | None = None
        for attempt in range(max_retries):
            raw = await self._call_llm(SYSTEM_PROMPT_EDIT, user_prompt, EDIT_RESPONSE_SCHEMA)
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
                new_xml = layout_bpmn(new_xml)
                return {"bpmn_xml": new_xml}

            logger.warning(f"Attempt {attempt + 1}: Invalid edited BPMN XML: {error}")
            user_prompt = (
                f"Your previous edit produced invalid BPMN XML. Error: {error}\n\n"
                f"Original BPMN XML:\n```xml\n{bpmn_xml}\n```\n\n"
                f"Modification instruction: {prompt}\n\n"
                f"Please fix the issue and try again."
            )

        raise ValueError(
            f"Failed to produce valid edited BPMN XML after {max_retries} attempts. "
            f"Last error: {error}"
        )

    async def classify(self, text: str) -> dict:
        """Classify whether input is a valid BPMN request."""
        logger.info("Calling LLM for input classification")
        raw = await self._call_llm(SYSTEM_PROMPT_CLASSIFY, text, CLASSIFY_RESPONSE_SCHEMA)
        result = self._extract_json(raw)
        return {
            "is_valid": bool(result.get("is_valid", False)),
            "reason": result.get("reason", ""),
        }

    async def close(self):
        await self.backend.close()
