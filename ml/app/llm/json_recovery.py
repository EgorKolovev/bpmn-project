"""JSON parsing + recovery paths for misshapen LLM responses.

Gemini flash-lite (and to a lesser extent other models) occasionally
emit JSON in non-standard shapes — double-escaped, structurally
escaped with stray quotes, or wrapped in chatty prose. Each recovery
path here corresponds to a real pathology observed in production logs.

Module-level functions; `LLMClient` exposes thin `_extract_json`,
`_repair_*`, and `_unescape_xml` instance methods that delegate here.
"""

import json
import re

import structlog

logger = structlog.get_logger(__name__)


def repair_double_escaped_json(text: str) -> str | None:
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
    if r"\"}" not in text and r"\"," not in text:
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
            text.replace(r"\"", '"').replace(r"\\", "\\").replace(r"\n", "\n").replace(r"\t", "\t")
        )


def repair_structural_escape(text: str) -> str | None:
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
    if r"\", \"" not in text and r"\",\"" not in text:
        return None
    candidate = text
    # Strip a single trailing stray `"` (the outer bookend).
    if candidate.endswith('"') and not candidate.endswith(r"\""):
        candidate = candidate[:-1]
    # Revert one level of escape on structural tokens only.
    repaired = (
        candidate.replace(r"\", \"", '", "')
        .replace(r"\",\"", '","')
        .replace(r"\": \"", '": "')
        .replace(r"\":\"", '":"')
        .replace(r"\"}", '"}')
        .replace(r": \"", ': "')
        .replace(r":\"", ':"')
    )
    return repaired


def extract_json(text: str) -> dict:
    """Parse LLM response text as JSON, exhausting recovery paths
    before raising `ValueError`."""
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
                    logger.info("Unwrapped stringified JSON from LLM response.")
                    return reparsed
            except json.JSONDecodeError:
                pass
            raise ValueError(f"LLM returned a plain string, not a JSON object (len={len(text)})")
        return parsed
    except json.JSONDecodeError as first_err:
        # Recovery path 1: Gemini sometimes double-escapes the entire JSON.
        repaired = repair_double_escaped_json(text)
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
        struct_repaired = repair_structural_escape(text)
        if struct_repaired is not None:
            try:
                result = json.loads(struct_repaired)
                logger.info("Recovered structurally-escaped JSON from LLM response.")
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
                    len(text),
                    first_err,
                    third_err,
                    text[:200],
                    text[-200:],
                )
                raise ValueError(
                    f"Could not parse JSON from LLM response " f"(len={len(text)}, err={third_err})"
                ) from third_err
        logger.error(
            "JSON parse failed, no outer braces. len=%d err=%s head=%r tail=%r",
            len(text),
            first_err,
            text[:200],
            text[-200:],
        )
        raise ValueError(f"Could not parse JSON from LLM response (len={len(text)})") from first_err


def unescape_xml(xml_str: str) -> str:
    """If the LLM wrapped the XML in JSON string-escape (\\\" etc.),
    decode one level. Bare XML passes through untouched."""
    if not xml_str.strip().startswith("<"):
        try:
            xml_str = json.loads(f'"{xml_str}"')
        except Exception:
            pass
    return xml_str
