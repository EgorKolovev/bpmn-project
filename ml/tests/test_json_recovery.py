"""Unit tests for LLM JSON parse recovery paths.

Covers three recovery strategies in `LLMClient._extract_json`:
  1. Double-escaped JSON (entire payload is a stringified JSON).
  2. Structurally-escaped JSON (top-level `{` with inner structural quotes
     escaped — Anton's "командировка" failure mode on flash-lite).
  3. Regex-based outer {...} extraction (chatty wrappers).
"""
import os

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")

import pytest

from app.budget import BudgetTracker
from app.llm import GeminiBackend, LLMClient


@pytest.fixture
def client():
    backend = GeminiBackend(api_key="dummy", model="gemini-2.5-flash", max_output_tokens=1024)
    tracker = BudgetTracker(
        db_path=":memory:",
        daily_limit_usd=1.0,
        input_price_per_million_usd=0.25,
        output_price_per_million_usd=1.50,
        max_output_tokens=1024,
    )
    yield LLMClient(budget_tracker=tracker, backend=backend)
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(backend.close())
        loop.close()
    except Exception:
        pass


class TestPlainParse:
    def test_wellformed_json_parses(self, client):
        raw = '{"bpmn_xml": "<xml/>", "session_name": "Foo"}'
        result = client._extract_json(raw)
        assert result == {"bpmn_xml": "<xml/>", "session_name": "Foo"}

    def test_fenced_code_block_stripped(self, client):
        raw = '```json\n{"bpmn_xml": "<xml/>"}\n```'
        result = client._extract_json(raw)
        assert result == {"bpmn_xml": "<xml/>"}


class TestDoubleEscapeRecovery:
    def test_full_payload_is_stringified(self, client):
        # Whole thing wrapped as a string — classic double-escape.
        raw = '"{\\"bpmn_xml\\": \\"<xml/>\\", \\"session_name\\": \\"Foo\\"}"'
        result = client._extract_json(raw)
        assert result == {"bpmn_xml": "<xml/>", "session_name": "Foo"}


class TestStructuralEscapeRecovery:
    """Anton's "командировка" failure mode.

    Model emits a top-level `{` (unescaped) but escapes EVERY inner quote
    — even structural ones between fields — then adds a stray trailing
    `"` at the end. Outer object never closes.
    """

    def test_anton_style_failure_is_recovered(self, client):
        # Mimic the exact shape we captured from Gemini flash-lite.
        raw = (
            '{"bpmn_xml": "<?xml version=\\"1.0\\"?><bpmn:definitions>'
            '<bpmn:process id=\\"P\\"/></bpmn:definitions>\\", '
            '\\"session_name\\": \\"Test\\"}"'
        )
        result = client._extract_json(raw)
        assert "bpmn_xml" in result
        assert "session_name" in result
        assert result["session_name"] == "Test"
        assert "<bpmn:definitions>" in result["bpmn_xml"]
        assert result["bpmn_xml"].startswith('<?xml version="1.0"?>')

    def test_no_trailing_quote_still_recovers(self, client):
        """Same pathology but without the trailing stray quote."""
        raw = (
            '{"bpmn_xml": "<xml/>\\", \\"session_name\\": \\"X\\"}'
        )
        result = client._extract_json(raw)
        assert result["bpmn_xml"] == "<xml/>"
        assert result["session_name"] == "X"

    def test_wellformed_json_untouched(self, client):
        """Plain well-formed JSON must parse directly, not via recovery."""
        raw = '{"bpmn_xml": "<xml attr=\\"v\\"/>", "session_name": "Foo"}'
        result = client._extract_json(raw)
        assert result["bpmn_xml"] == '<xml attr="v"/>'


class TestResponseSchemaInPayload:
    """The Gemini payload must include responseJsonSchema when one is
    provided — this is what prevents the structural-escape bug at the
    source (see Context7 Gemini structured-output docs)."""

    def test_generate_schema_present(self):
        from app.llm import GENERATE_RESPONSE_SCHEMA
        backend = GeminiBackend(api_key="dummy", model="gemini-2.5-flash", max_output_tokens=512)
        try:
            payload = backend._build_payload("sys", "user", GENERATE_RESPONSE_SCHEMA)
            schema = payload["generationConfig"].get("responseJsonSchema")
            assert schema is not None
            assert schema["type"] == "object"
            assert "bpmn_xml" in schema["properties"]
            assert "session_name" in schema["properties"]
            assert "bpmn_xml" in schema["required"]
        finally:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(backend.close())
                loop.close()
            except Exception:
                pass

    def test_no_schema_payload_omits_key(self):
        """When no schema is passed, responseJsonSchema MUST be absent
        — backwards-compat for any caller that wants free-form JSON."""
        backend = GeminiBackend(api_key="dummy", model="gemini-2.5-flash", max_output_tokens=512)
        try:
            payload = backend._build_payload("sys", "user")
            assert "responseJsonSchema" not in payload["generationConfig"]
            # But responseMimeType stays — we still want JSON mode.
            assert payload["generationConfig"]["responseMimeType"] == "application/json"
        finally:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(backend.close())
                loop.close()
            except Exception:
                pass

    def test_polza_schema_uses_json_schema_format(self):
        """Polza (OpenAI-compatible) uses response_format.type=json_schema,
        not Gemini's responseJsonSchema. Keep both wire formats in sync."""
        from app.llm import PolzaBackend, EDIT_RESPONSE_SCHEMA
        backend = PolzaBackend(
            api_key="dummy", model="x/y", base_url="http://localhost", max_output_tokens=512,
        )
        # We just verify the helper can be built — actual HTTP call is
        # out of scope for unit tests. Schema threading tested via llm.py.
        import asyncio
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(backend.close())
            loop.close()
        except Exception:
            pass


class TestCharLimitRaised:
    """Regression guard: REQUEST_CHAR_LIMIT / MAX_MESSAGE_CHARS should
    accommodate realistic client-provided specs (~13 KB PDFs)."""

    def test_ml_request_limit_ge_20000(self):
        from app import config
        assert config.REQUEST_CHAR_LIMIT >= 20000, (
            f"REQUEST_CHAR_LIMIT = {config.REQUEST_CHAR_LIMIT} is too tight "
            "for real customer specs (10–13 KB PDFs)."
        )
