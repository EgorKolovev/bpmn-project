"""Level 1 i18n unit tests.

These are mock-based tests that verify FastAPI/JSON/Pydantic transport
correctly preserves UTF-8 (Cyrillic) bytes in /classify, /generate, /edit
responses. They don't exercise the LLM — just the endpoint plumbing.

If these fail, there's a transport / serialization bug (unrelated to prompt
quality). They always run fast and have no external dependencies.
"""
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import VALID_BPMN_XML_NO_DI, cyrillic_ratio


os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")

from app.main import app  # noqa: E402
import app.main as _main  # noqa: E402

# Derive the API key the middleware will verify against; tests attach this
# as a default header on TestClient so middleware passes.
_INTERNAL_KEY = _main.INTERNAL_API_KEY


RU_VALID_REASON = "Это не описание бизнес-процесса."


VALID_BPMN_XML_RU = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1" name="Начало">
      <bpmn:outgoing>Flow_1</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_1" name="Создать заявку">
      <bpmn:incoming>Flow_1</bpmn:incoming>
      <bpmn:outgoing>Flow_2</bpmn:outgoing>
    </bpmn:task>
    <bpmn:task id="Task_2" name="Проверка документов">
      <bpmn:incoming>Flow_2</bpmn:incoming>
      <bpmn:outgoing>Flow_3</bpmn:outgoing>
    </bpmn:task>
    <bpmn:endEvent id="End_1" name="Завершение">
      <bpmn:incoming>Flow_3</bpmn:incoming>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="Flow_3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""


@pytest.fixture
def client():
    headers = {"X-Internal-Api-Key": _INTERNAL_KEY} if _INTERNAL_KEY else {}
    return TestClient(app, headers=headers)


class TestClassifyUtf8RoundTrip:
    """Ensure Cyrillic in `reason` survives FastAPI JSON serialization."""

    def test_classify_valid_russian_input(self, client):
        mock_result = {"is_valid": True, "reason": ""}
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.classify = AsyncMock(return_value=mock_result)
            response = client.post(
                "/classify",
                json={"text": "Процесс согласования договора."},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["is_valid"] is True
        assert data["reason"] == ""

    def test_classify_invalid_with_cyrillic_reason(self, client):
        mock_result = {"is_valid": False, "reason": RU_VALID_REASON}
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.classify = AsyncMock(return_value=mock_result)
            response = client.post(
                "/classify",
                json={"text": "Какая сегодня погода?"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["is_valid"] is False
        # Byte-exact preservation: no mojibake, no escape sequences
        assert data["reason"] == RU_VALID_REASON
        # Reason should be dominantly Cyrillic
        assert cyrillic_ratio(data["reason"]) >= 0.5

    def test_classify_body_is_utf8_not_ascii_escaped(self, client):
        """FastAPI should send UTF-8 bytes, not \\uXXXX sequences."""
        mock_result = {"is_valid": False, "reason": RU_VALID_REASON}
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.classify = AsyncMock(return_value=mock_result)
            response = client.post(
                "/classify",
                json={"text": "абырвалг"},
            )
        assert response.status_code == 200
        # Raw bytes should contain the actual Cyrillic UTF-8, not \uXXXX
        raw = response.content.decode("utf-8")
        assert "Это не описание" in raw
        # Should NOT contain \u-escaped form
        assert "\\u0421" not in raw  # `С` escaped


class TestGenerateUtf8RoundTrip:
    def test_generate_russian_names_roundtrip(self, client):
        mock_result = {
            "bpmn_xml": VALID_BPMN_XML_RU,
            "session_name": "Согласование договора",
        }
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.generate = AsyncMock(return_value=mock_result)
            response = client.post(
                "/generate",
                json={"description": "Процесс согласования договора."},
            )
        assert response.status_code == 200
        data = response.json()
        assert "Создать заявку" in data["bpmn_xml"]
        assert "Проверка документов" in data["bpmn_xml"]
        assert data["session_name"] == "Согласование договора"
        assert cyrillic_ratio(data["session_name"]) >= 0.7


class TestEditUtf8RoundTrip:
    def test_edit_russian_names_roundtrip(self, client):
        mock_result = {"bpmn_xml": VALID_BPMN_XML_RU}
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.edit = AsyncMock(return_value=mock_result)
            response = client.post(
                "/edit",
                json={
                    "prompt": "Добавь шаг архивации",
                    "bpmn_xml": VALID_BPMN_XML_NO_DI,
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert "Создать заявку" in data["bpmn_xml"]


class TestHelpers:
    """Unit tests for the cyrillic_ratio / extract_names helpers themselves."""

    def test_cyrillic_ratio_pure_russian(self):
        assert cyrillic_ratio("Проверка документов") >= 0.95

    def test_cyrillic_ratio_pure_english(self):
        assert cyrillic_ratio("Review documents") < 0.01

    def test_cyrillic_ratio_mixed_half(self):
        r = cyrillic_ratio("ab вг")
        assert 0.45 < r < 0.55

    def test_cyrillic_ratio_empty_and_whitespace(self):
        assert cyrillic_ratio("") == 0.0
        assert cyrillic_ratio("   ") == 0.0
        assert cyrillic_ratio("123 !@#") == 0.0

    def test_cyrillic_ratio_ignores_digits_and_punct(self):
        # "Задача 1" — only "Задача" counts as letters, all Cyrillic
        assert cyrillic_ratio("Задача 1") == 1.0

    def test_extract_flow_node_names(self):
        from tests.conftest import extract_flow_node_names

        names = extract_flow_node_names(VALID_BPMN_XML_RU)
        assert set(names) == {
            "Начало",
            "Создать заявку",
            "Проверка документов",
            "Завершение",
        }

    def test_extract_flow_node_names_skips_sequence_flows(self):
        from tests.conftest import extract_flow_node_names

        # VALID_BPMN_XML_NO_DI has no sequenceFlow names — make sure they
        # aren't accidentally extracted
        names = extract_flow_node_names(VALID_BPMN_XML_NO_DI)
        # Should contain task/event names, not flow ids like Flow_1
        for n in names:
            assert not n.startswith("Flow_")
