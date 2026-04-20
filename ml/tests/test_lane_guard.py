"""Unit tests for the explicit-role-hint lane guard.

Covers:
  * `description_requires_lanes` correctly classifies common RU/EN triggers.
  * `xml_has_lanes` detects both `<bpmn:laneSet>` and `<laneSet>` (no prefix).
  * Both helpers are conservative — they don't false-positive on casual
    mentions like "employee role in the system".

These are pure-function tests; no LLM calls.
"""
import os

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")

import pytest

from app.llm import description_requires_lanes, xml_has_lanes


class TestDescriptionRequiresLanes:
    @pytest.mark.parametrize("desc", [
        "Используй роли: сотрудник, директор.",
        "используй роли ниже",
        "Схема в 3 ролях: A, B, C",
        "Процесс в 2 ролях",
        "Роли: менеджер, юрист, директор",
        "Участники: Alice, Bob",
        "Актёры: X, Y",
        "Use roles: manager, legal, director",
        "The process runs in 4 roles",
        "Actors: HR, Legal",
        "Participants: customer, support",
        "Group by role",
        "With roles assigned to each step",
        "Swimlanes: sales, legal, finance",
    ])
    def test_positive_triggers(self, desc):
        assert description_requires_lanes(desc), (
            f"Expected {desc!r} to trigger lane requirement"
        )

    @pytest.mark.parametrize("desc", [
        "Employee onboarding process with HR approval",
        "Order fulfillment: receive, pack, ship.",
        "Процесс согласования договора: менеджер создаёт заявку.",
        "The customer submits a ticket and waits.",
        # "role" in casual context — NOT an enumeration trigger
        "The manager's role in this process is to approve.",
        "User registration with email confirmation.",
    ])
    def test_negative_no_false_positives(self, desc):
        assert not description_requires_lanes(desc), (
            f"{desc!r} should NOT trigger the lane requirement"
        )


class TestXmlHasLanes:
    def test_detects_namespaced(self):
        xml = '<bpmn:process><bpmn:laneSet id="L"/></bpmn:process>'
        assert xml_has_lanes(xml)

    def test_detects_unprefixed(self):
        xml = '<process><laneSet id="L"/></process>'
        assert xml_has_lanes(xml)

    def test_detects_with_whitespace(self):
        xml = '<bpmn:process>\n  <bpmn:laneSet   id="L"/>\n</bpmn:process>'
        assert xml_has_lanes(xml)

    def test_rejects_no_lanes(self):
        xml = '<bpmn:process><bpmn:task id="T"/></bpmn:process>'
        assert not xml_has_lanes(xml)

    def test_rejects_similar_name(self):
        # `laneRef` / `landing` should not match `laneSet`
        xml = '<bpmn:process><bpmn:landingZone/></bpmn:process>'
        assert not xml_has_lanes(xml)


class TestAntonPromptTriggersLanes:
    """Anton's exact prompt from the client chat — must trigger lane guard."""

    ANTON_PROMPT = (
        "Используй роли:\n\n"
        "Схема Fly описывает процесс командировки в 3 ролях: сотрудник, "
        "руководитель/CEO ЦФО и бухгалтерия/кадры. Сотрудник инициирует процесс."
    )

    def test_triggers(self):
        assert description_requires_lanes(self.ANTON_PROMPT)
