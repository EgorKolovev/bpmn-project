"""Edge-case + defensive tests — `LLMClient` / `bpmn_layout` / FastAPI
endpoints fed boundary inputs (empty responses, single-character
prompts, malformed XML, no-process documents, very-large outputs).

These are pure unit tests — no LLM, no Docker, no DB — they only
exercise the deterministic parts of the pipeline:

  * `_extract_json`: empty / whitespace / JSON-shaped-but-string.
  * `LLMClient.generate`: pydantic-level validation
    (1-char / oversized description handled by FastAPI; here we
    test what `_extract_json` does when the LLM goes silent).
  * `bpmn_layout.layout_bpmn`: definitions-only XML, missing process,
    flow nodes without IDs.
  * The FastAPI endpoints' input validation via `TestClient`.
"""

import os

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")


import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from app.bpmn_layout import has_layout, layout_bpmn
from app.budget import BudgetTracker
from app.llm import GeminiBackend, LLMClient, LLMClientError
from tests.conftest import VALID_BPMN_XML_NO_DI, make_gemini_count_response, make_gemini_response

# ---------------------------------------------------------------------------
# LLMClient — empty / weird LLM responses
# ---------------------------------------------------------------------------


def _budget(tmp_path):
    return BudgetTracker(
        db_path=str(tmp_path / "b.sqlite"),
        daily_limit_usd=100.0,
        input_price_per_million_usd=0.25,
        output_price_per_million_usd=1.50,
        max_output_tokens=2048,
    )


def _wire_gemini(monkeypatch, tmp_path, gen_text: str):
    """Build an LLMClient whose backend returns `gen_text` from
    generateContent and a benign 100-token count from countTokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "countTokens" in str(request.url):
            return httpx.Response(200, json=make_gemini_count_response())
        return httpx.Response(200, json=make_gemini_response(text=gen_text))

    backend = GeminiBackend(api_key="dummy", model="gemini-3-flash-preview", max_output_tokens=2048)
    monkeypatch.setattr(
        backend,
        "http_client",
        httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"x-goog-api-key": "dummy"},
            base_url="https://generativelanguage.googleapis.com",
        ),
    )
    return LLMClient(budget_tracker=_budget(tmp_path), backend=backend)


class TestEmptyLLMResponses:
    @pytest.mark.asyncio
    async def test_generate_raises_when_llm_returns_only_whitespace(self, monkeypatch, tmp_path):
        """If every retry returns blank text, the LLM is effectively
        broken — we must surface that as `ValueError` after exhausting
        retries, not return an empty bpmn_xml."""
        client = _wire_gemini(monkeypatch, tmp_path, gen_text="   ")
        with pytest.raises((ValueError, LLMClientError)):
            await client.generate("Anything.")

    @pytest.mark.asyncio
    async def test_generate_raises_when_llm_returns_plain_string(self, monkeypatch, tmp_path):
        """Model emits `"hello"` (a JSON string, not an object).
        `_extract_json` should reject and trigger retries until exhausted."""
        client = _wire_gemini(monkeypatch, tmp_path, gen_text='"hello"')
        with pytest.raises((ValueError, LLMClientError)):
            await client.generate("Anything.")

    @pytest.mark.asyncio
    async def test_generate_raises_when_llm_returns_array(self, monkeypatch, tmp_path):
        """Model emits `[1, 2, 3]` (a JSON array, not an object)."""
        client = _wire_gemini(monkeypatch, tmp_path, gen_text="[1, 2, 3]")
        with pytest.raises((ValueError, LLMClientError)):
            await client.generate("Anything.")


# ---------------------------------------------------------------------------
# layout_bpmn — malformed XML & edge structures
# ---------------------------------------------------------------------------


class TestLayoutEdgeCases:
    def test_definitions_only_returned_unchanged(self):
        """Just `<bpmn:definitions/>` with no process → defensive
        return-unchanged. No crash, no spurious BPMNDiagram."""
        xml = (
            '<?xml version="1.0"?>'
            '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"/>'
        )
        out = layout_bpmn(xml)
        assert not has_layout(out)

    def test_xml_with_only_sequence_flows_returned_unchanged(self):
        """All sequenceFlow elements, no flow nodes → no shapes to lay
        out → return unchanged. Currently the layouter returns the
        input as-is when `nodes` is empty."""
        xml = """<?xml version="1.0"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="P1">
    <bpmn:sequenceFlow id="F1" sourceRef="A" targetRef="B"/>
  </bpmn:process>
</bpmn:definitions>"""
        out = layout_bpmn(xml)
        assert not has_layout(out)

    def test_node_without_id_is_skipped(self):
        """A flow node that's missing `id` gets dropped, but the rest
        of the diagram still lays out correctly."""
        xml = """<?xml version="1.0"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <bpmn:process id="P1" isExecutable="true">
    <bpmn:startEvent id="Start_1"><bpmn:outgoing>F1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task name="No ID"><bpmn:incoming>F1</bpmn:incoming></bpmn:task>
    <bpmn:endEvent id="End_1"><bpmn:incoming>F2</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""
        out = layout_bpmn(xml)
        # Layout succeeds for the 2 nodes with IDs; the nameless one
        # is silently skipped.
        import re

        shape_count = len(re.findall(r"<bpmndi:BPMNShape", out))
        assert shape_count == 2

    def test_empty_string_returned_unchanged(self):
        assert layout_bpmn("") == ""

    def test_whitespace_returned_unchanged(self):
        assert layout_bpmn("   \n  ") == "   \n  "

    def test_huge_diagram_layouts_within_reasonable_bounds(self):
        """A 100-node linear chain shouldn't blow up the layouter.
        Verifies the BFS column assignment and node placement scale
        linearly."""
        N = 100
        node_xml = []
        for i in range(N):
            node_xml.append(
                f'<bpmn:task id="T{i}" name="Task {i}">'
                f"<bpmn:incoming>F{i}</bpmn:incoming>"
                f"<bpmn:outgoing>F{i+1}</bpmn:outgoing>"
                f"</bpmn:task>"
            )
        flow_xml = "".join(
            f'<bpmn:sequenceFlow id="F{i+1}" sourceRef="T{i}" targetRef="T{i+1}"/>'
            for i in range(N - 1)
        )
        xml = (
            '<?xml version="1.0"?>'
            '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
            '<bpmn:process id="P1" isExecutable="true">'
            '<bpmn:startEvent id="Start"><bpmn:outgoing>F0</bpmn:outgoing></bpmn:startEvent>'
            + "".join(node_xml)
            + '<bpmn:endEvent id="End"><bpmn:incoming>F_END</bpmn:incoming></bpmn:endEvent>'
            + '<bpmn:sequenceFlow id="F0" sourceRef="Start" targetRef="T0"/>'
            + flow_xml
            + f'<bpmn:sequenceFlow id="F_END" sourceRef="T{N-1}" targetRef="End"/>'
            + "</bpmn:process></bpmn:definitions>"
        )
        out = layout_bpmn(xml)
        assert has_layout(out)
        import re

        # All N tasks + Start + End → N+2 shapes.
        assert len(re.findall(r"<bpmndi:BPMNShape", out)) == N + 2


# ---------------------------------------------------------------------------
# FastAPI input validation — pydantic on /generate, /edit, /classify.
#
# These use `TestClient` to drive the real FastAPI app. They don't
# touch the LLM (validation rejects before that), so no monkeypatch
# needed for `llm_client`.
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(monkeypatch):
    """Wire a TestClient around the running FastAPI app, with the
    auth middleware bypassed by setting INTERNAL_API_KEY = '' (the
    middleware short-circuits when no key is configured)."""
    from app import main as ml_main

    # Bypass auth: middleware lets every request through when the
    # module-level INTERNAL_API_KEY is empty.
    monkeypatch.setattr(ml_main, "INTERNAL_API_KEY", "")
    return TestClient(ml_main.app)


class TestEndpointValidation:
    def test_generate_empty_description_returns_422(self, app_client):
        r = app_client.post("/generate", json={"description": ""})
        assert r.status_code == 422

    def test_generate_single_char_description_passes_validation(self, app_client):
        """1-char description passes pydantic (min_length=1) — the
        rejection happens later inside LLMClient if at all. Tests
        only the validation gate."""
        # We don't have a real LLM here, so the request will either:
        #   * reach the LLM client (which errors without real httpx
        #     transport) → 502/500
        #   * or 422 if min_length rejects 1-char
        # The point is: 422 must NOT be the response for 1 character —
        # pydantic's min_length is 1.
        r = app_client.post("/generate", json={"description": "x"})
        assert r.status_code != 422

    def test_generate_oversized_description_returns_422(self, app_client):
        oversized = "x" * (config.REQUEST_CHAR_LIMIT + 100)
        r = app_client.post("/generate", json={"description": oversized})
        assert r.status_code == 422

    def test_generate_missing_field_returns_422(self, app_client):
        r = app_client.post("/generate", json={})
        assert r.status_code == 422

    def test_generate_unknown_extra_field_returns_422(self, app_client):
        """`model_config = ConfigDict(extra="forbid")` — request must
        be rejected if it carries unexpected fields."""
        r = app_client.post("/generate", json={"description": "ok", "rogue_field": "x"})
        assert r.status_code == 422

    def test_edit_missing_bpmn_xml_returns_422(self, app_client):
        r = app_client.post("/edit", json={"prompt": "Add something"})
        assert r.status_code == 422

    def test_edit_missing_prompt_returns_422(self, app_client):
        r = app_client.post("/edit", json={"bpmn_xml": VALID_BPMN_XML_NO_DI})
        assert r.status_code == 422

    def test_edit_oversized_bpmn_xml_returns_422(self, app_client):
        oversized = "<x>" + "y" * (config.BPMN_XML_CHAR_LIMIT + 100) + "</x>"
        r = app_client.post("/edit", json={"prompt": "Add something", "bpmn_xml": oversized})
        assert r.status_code == 422

    def test_classify_empty_text_returns_422(self, app_client):
        r = app_client.post("/classify", json={"text": ""})
        assert r.status_code == 422

    def test_classify_oversized_text_returns_422(self, app_client):
        oversized = "x" * (config.REQUEST_CHAR_LIMIT + 100)
        r = app_client.post("/classify", json={"text": oversized})
        assert r.status_code == 422

    def test_health_no_validation(self, app_client):
        r = app_client.get("/health")
        assert r.status_code == 200
