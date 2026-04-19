import json
import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from app.budget import DailyBudgetExceededError
from app.llm import LLMClientError
from tests.conftest import VALID_BPMN_XML


os.environ["GEMINI_API_KEY"] = "test-key-for-unit-tests"

from app.main import app
import app.main as _main

# Derive the API key the middleware will verify against; tests attach this
# as a default header on TestClient so middleware passes.
_INTERNAL_KEY = _main.INTERNAL_API_KEY


@pytest.fixture
def client():
    headers = {"X-Internal-Api-Key": _INTERNAL_KEY} if _INTERNAL_KEY else {}
    return TestClient(app, headers=headers)


class TestHealthEndpoint:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestGenerateEndpoint:
    def test_generate_success(self, client):
        mock_result = {"bpmn_xml": VALID_BPMN_XML, "session_name": "Hiring Process"}
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.generate = AsyncMock(return_value=mock_result)
            response = client.post(
                "/generate",
                json={"description": "A simple hiring process"},
            )
        assert response.status_code == 200
        data = response.json()
        assert "bpmn_xml" in data
        assert "session_name" in data
        assert data["session_name"] == "Hiring Process"

    def test_generate_empty_description(self, client):
        response = client.post("/generate", json={"description": ""})
        assert response.status_code == 422

    def test_generate_missing_description(self, client):
        response = client.post("/generate", json={})
        assert response.status_code == 422

    def test_generate_llm_failure(self, client):
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.generate = AsyncMock(side_effect=LLMClientError("LLM failed"))
            response = client.post(
                "/generate",
                json={"description": "Some process"},
            )
        assert response.status_code == 502
        assert "LLM failed" in response.json()["detail"]

    def test_generate_daily_budget_exceeded(self, client):
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.generate = AsyncMock(
                side_effect=DailyBudgetExceededError(5.0, "2026-03-07", "UTC")
            )
            response = client.post(
                "/generate",
                json={"description": "Some process"},
            )
        assert response.status_code == 429
        assert "$5.00" in response.json()["detail"]


class TestEditEndpoint:
    def test_edit_success(self, client):
        mock_result = {"bpmn_xml": VALID_BPMN_XML}
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.edit = AsyncMock(return_value=mock_result)
            response = client.post(
                "/edit",
                json={
                    "prompt": "Add a review step",
                    "bpmn_xml": VALID_BPMN_XML,
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert "bpmn_xml" in data

    def test_edit_empty_prompt(self, client):
        response = client.post(
            "/edit",
            json={"prompt": "", "bpmn_xml": VALID_BPMN_XML},
        )
        assert response.status_code == 422

    def test_edit_empty_xml(self, client):
        response = client.post(
            "/edit",
            json={"prompt": "Edit something", "bpmn_xml": ""},
        )
        assert response.status_code == 422

    def test_edit_missing_fields(self, client):
        response = client.post("/edit", json={"prompt": "Edit"})
        assert response.status_code == 422

    def test_edit_llm_failure(self, client):
        with patch("app.main.llm_client") as mock_llm:
            mock_llm.edit = AsyncMock(side_effect=LLMClientError("Edit failed"))
            response = client.post(
                "/edit",
                json={
                    "prompt": "Change something",
                    "bpmn_xml": VALID_BPMN_XML,
                },
            )
        assert response.status_code == 502
        assert "Edit failed" in response.json()["detail"]
