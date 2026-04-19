"""Level 2 i18n E2E tests — hit the real ML service with real LLM calls.

These tests verify that our prompt engineering actually produces
language-matching output. They are slow (several seconds each) and cost
real LLM tokens. They require a running ML service reachable via HTTP.

Skipped unless RUN_E2E=1. Typical invocation (inside docker ml container):

    RUN_E2E=1 pytest tests/test_i18n_e2e.py -v

Each test has heuristic assertions on Cyrillic ratio rather than exact
string matches because the LLM is non-deterministic. The
pytest-rerunfailures plugin retries transient failures up to 2 times.
"""
import os

import httpx
import pytest

from tests.conftest import (
    cyrillic_ratio,
    extract_flow_node_names,
)


RUN_E2E = os.environ.get("RUN_E2E", "0") == "1"
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not RUN_E2E, reason="Set RUN_E2E=1 to run E2E tests"),
    pytest.mark.flaky(reruns=2, reruns_delay=2),
]


# ---------------------------------------------------------------------------
# Test data — stable process descriptions used across tests
# ---------------------------------------------------------------------------

RU_VALID_PROCESS = (
    "Процесс согласования договора: менеджер создаёт заявку, "
    "юрист проверяет документы, директор подписывает."
)
RU_VALID_TRAVEL = (
    "Оформление командировки сотрудника: подача заявки на командировку, "
    "утверждение руководителем, оформление билетов бухгалтерией."
)
RU_WEATHER = "Какая сегодня погода в Москве?"
RU_GREETING = "Привет, как дела?"
RU_NOISE = "абырвалг"
EN_VALID_PROCESS = (
    "Order fulfillment process: customer places order, "
    "payment is verified, item is picked from warehouse, "
    "package is shipped to the customer."
)
EN_WEATHER = "What is the weather today?"
MIXED_VALID = "Process for согласование договора with юрист approval"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def ml_client(ml_base_url, internal_api_key):
    """Async HTTP client pointed at the ML service with auth header."""
    headers = {}
    if internal_api_key:
        headers["X-Internal-Api-Key"] = internal_api_key
    async with httpx.AsyncClient(
        base_url=ml_base_url,
        headers=headers,
        timeout=120.0,
    ) as client:
        yield client


async def _classify(client: httpx.AsyncClient, text: str) -> dict:
    resp = await client.post("/classify", json={"text": text})
    assert resp.status_code == 200, f"/classify failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _generate(client: httpx.AsyncClient, description: str) -> dict:
    resp = await client.post("/generate", json={"description": description})
    assert resp.status_code == 200, f"/generate failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _edit(client: httpx.AsyncClient, prompt: str, bpmn_xml: str) -> dict:
    resp = await client.post("/edit", json={"prompt": prompt, "bpmn_xml": bpmn_xml})
    assert resp.status_code == 200, f"/edit failed: {resp.status_code} {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Classification — language support
# ---------------------------------------------------------------------------


class TestClassifyI18n:
    async def test_russian_valid_business_process(self, ml_client):
        result = await _classify(ml_client, RU_VALID_PROCESS)
        assert result["is_valid"] is True, (
            f"Russian business process must be classified as valid, got {result}"
        )

    async def test_russian_valid_travel_process(self, ml_client):
        result = await _classify(ml_client, RU_VALID_TRAVEL)
        assert result["is_valid"] is True, (
            f"Russian travel process must be classified as valid, got {result}"
        )

    async def test_russian_invalid_weather(self, ml_client):
        result = await _classify(ml_client, RU_WEATHER)
        assert result["is_valid"] is False, (
            f"Weather question must be classified as invalid, got {result}"
        )
        reason = result.get("reason", "")
        assert reason, "Reason must not be empty for invalid input"
        assert cyrillic_ratio(reason) >= 0.5, (
            f"Reason for Russian input must be in Russian, got: {reason!r} "
            f"(cyrillic_ratio={cyrillic_ratio(reason):.2f})"
        )

    async def test_russian_invalid_greeting(self, ml_client):
        result = await _classify(ml_client, RU_GREETING)
        assert result["is_valid"] is False, (
            f"Greeting must be classified as invalid, got {result}"
        )
        reason = result.get("reason", "")
        assert reason
        assert cyrillic_ratio(reason) >= 0.5

    async def test_russian_short_noise(self, ml_client):
        result = await _classify(ml_client, RU_NOISE)
        assert result["is_valid"] is False, (
            f"Gibberish must be classified as invalid, got {result}"
        )

    async def test_english_valid_regression(self, ml_client):
        """Regression: English input must still work."""
        result = await _classify(ml_client, EN_VALID_PROCESS)
        assert result["is_valid"] is True, (
            f"English business process must be valid, got {result}"
        )

    async def test_english_invalid_reason_in_english(self, ml_client):
        """Reason for English input must be English, not translated."""
        result = await _classify(ml_client, EN_WEATHER)
        assert result["is_valid"] is False
        reason = result.get("reason", "")
        assert reason
        assert cyrillic_ratio(reason) < 0.1, (
            f"Reason for English input must be in English, got: {reason!r} "
            f"(cyrillic_ratio={cyrillic_ratio(reason):.2f})"
        )

    async def test_mixed_language_valid(self, ml_client):
        """Mixed-language business process description should be accepted."""
        result = await _classify(ml_client, MIXED_VALID)
        assert result["is_valid"] is True, (
            f"Mixed-language process description must be valid, got {result}"
        )


# ---------------------------------------------------------------------------
# Generation — language matching in `name` attributes
# ---------------------------------------------------------------------------


class TestGenerateI18n:
    async def test_russian_labels_in_flow_nodes(self, ml_client):
        result = await _generate(ml_client, RU_VALID_PROCESS)
        xml = result["bpmn_xml"]
        names = extract_flow_node_names(xml)
        assert names, f"Generated XML has no named flow nodes: {xml[:500]}"

        low_cyrillic = [
            (n, round(cyrillic_ratio(n), 2))
            for n in names
            if cyrillic_ratio(n) < 0.7
        ]
        assert not low_cyrillic, (
            f"Russian input should produce Russian names; offenders: {low_cyrillic}. "
            f"All names: {names}"
        )

    async def test_russian_session_name(self, ml_client):
        result = await _generate(ml_client, RU_VALID_PROCESS)
        session_name = result.get("session_name", "")
        assert session_name, "session_name must be non-empty"
        assert cyrillic_ratio(session_name) >= 0.7, (
            f"Russian input must produce Russian session_name, got: "
            f"{session_name!r} (ratio={cyrillic_ratio(session_name):.2f})"
        )

    async def test_english_labels_regression(self, ml_client):
        """Regression: English input still produces English names."""
        result = await _generate(ml_client, EN_VALID_PROCESS)
        xml = result["bpmn_xml"]
        names = extract_flow_node_names(xml)
        assert names, f"Generated XML has no named flow nodes: {xml[:500]}"

        high_cyrillic = [
            (n, round(cyrillic_ratio(n), 2))
            for n in names
            if cyrillic_ratio(n) > 0.1
        ]
        assert not high_cyrillic, (
            f"English input must produce English names; offenders: {high_cyrillic}. "
            f"All names: {names}"
        )

    async def test_russian_travel_process_labels(self, ml_client):
        """Second Russian test case — different process, same expectation."""
        result = await _generate(ml_client, RU_VALID_TRAVEL)
        xml = result["bpmn_xml"]
        names = extract_flow_node_names(xml)
        assert names

        low_cyrillic = [
            (n, round(cyrillic_ratio(n), 2))
            for n in names
            if cyrillic_ratio(n) < 0.7
        ]
        assert not low_cyrillic, (
            f"Travel process must have Russian names; offenders: {low_cyrillic}"
        )


# ---------------------------------------------------------------------------
# Edit — language preservation
# ---------------------------------------------------------------------------


class TestEditI18n:
    async def test_edit_russian_diagram_with_russian_instruction(self, ml_client):
        """Generate Russian diagram, then edit with Russian instruction.

        All names (existing + new) must stay in Russian.
        """
        gen = await _generate(ml_client, RU_VALID_PROCESS)
        original_xml = gen["bpmn_xml"]
        original_names = extract_flow_node_names(original_xml)

        edited = await _edit(
            ml_client,
            "Добавь шаг архивации документов в конце процесса",
            original_xml,
        )
        new_xml = edited["bpmn_xml"]
        new_names = extract_flow_node_names(new_xml)

        assert len(new_names) >= len(original_names), (
            f"Edit should add a node; old: {original_names}, new: {new_names}"
        )
        low_cyrillic = [
            (n, round(cyrillic_ratio(n), 2))
            for n in new_names
            if cyrillic_ratio(n) < 0.7
        ]
        assert not low_cyrillic, (
            f"Russian diagram + Russian instruction → all Russian names. "
            f"Offenders: {low_cyrillic}. All new: {new_names}"
        )

    async def test_edit_russian_diagram_with_english_instruction(self, ml_client):
        """Diagram is Russian; instruction is English.

        Prompt says: follow diagram's dominant language (Russian) for new nodes.
        Existing names must be preserved verbatim.
        """
        gen = await _generate(ml_client, RU_VALID_PROCESS)
        original_xml = gen["bpmn_xml"]
        original_names = set(extract_flow_node_names(original_xml))

        edited = await _edit(
            ml_client,
            "Add an archive step at the end of the process",
            original_xml,
        )
        new_xml = edited["bpmn_xml"]
        new_names = extract_flow_node_names(new_xml)

        # Every ORIGINAL name must still be present (preserved verbatim)
        missing = original_names - set(new_names)
        assert not missing, (
            f"Edit destroyed original Russian names: {missing}. "
            f"New names: {new_names}"
        )

        # Existing (preserved) names are all Russian — at least those that
        # overlap with original_names must still have cyrillic_ratio >= 0.7
        preserved = [n for n in new_names if n in original_names]
        bad_preserved = [n for n in preserved if cyrillic_ratio(n) < 0.7]
        assert not bad_preserved, (
            f"Preserved names should be Russian: {bad_preserved}"
        )
