"""Public API for the `app.llm` package.

Every symbol that consumers (`app.main`, test files) used to import
from the monolithic `app.llm` module is re-exported here, so the
package split is invisible to the rest of the codebase.
"""

from app.llm.backends import (
    GEMINI_API_URL,
    Backend,
    GeminiBackend,
    PolzaBackend,
    _map_budget_to_effort,
)
from app.llm.client import LLMClient
from app.llm.errors import LLMClientError
from app.llm.json_recovery import (
    extract_json as _extract_json,
    repair_double_escaped_json as _repair_double_escaped_json,
    repair_structural_escape as _repair_structural_escape,
    unescape_xml as _unescape_xml,
)
from app.llm.lane_guard import description_requires_lanes, xml_has_lanes
from app.llm.schemas import (
    CLASSIFY_RESPONSE_SCHEMA,
    EDIT_RESPONSE_SCHEMA,
    GENERATE_RESPONSE_SCHEMA,
)

__all__ = [
    "CLASSIFY_RESPONSE_SCHEMA",
    "EDIT_RESPONSE_SCHEMA",
    "GENERATE_RESPONSE_SCHEMA",
    "GEMINI_API_URL",
    "Backend",
    "GeminiBackend",
    "LLMClient",
    "LLMClientError",
    "PolzaBackend",
    "_extract_json",
    "_map_budget_to_effort",
    "_repair_double_escaped_json",
    "_repair_structural_escape",
    "_unescape_xml",
    "description_requires_lanes",
    "xml_has_lanes",
]
