"""Response JSON schemas for strict structured output.

Gemini's `responseJsonSchema` + Polza's OpenAI-compatible
`response_format.json_schema` both consume these objects. Passing
`responseMimeType=application/json` *without* a schema was observed
to produce double-escaped payloads on long XML outputs (Gemini wrapped
the whole response in extra string quoting). With an explicit schema
the model is constrained to emit a real object.
"""

from typing import Any

GENERATE_RESPONSE_SCHEMA: dict[str, Any] = {
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

EDIT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bpmn_xml": {
            "type": "string",
            "description": "The updated BPMN 2.0 XML document.",
        },
    },
    "required": ["bpmn_xml"],
}

CLASSIFY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_valid": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_valid"],
}
