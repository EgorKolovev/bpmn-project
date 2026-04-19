import os
import re

import pytest
from defusedxml import ElementTree as ET


VALID_BPMN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
                  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
                  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1" name="Start">
      <bpmn:outgoing>Flow_1</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_1" name="Receive Application">
      <bpmn:incoming>Flow_1</bpmn:incoming>
      <bpmn:outgoing>Flow_2</bpmn:outgoing>
    </bpmn:task>
    <bpmn:task id="Task_2" name="Review Application">
      <bpmn:incoming>Flow_2</bpmn:incoming>
      <bpmn:outgoing>Flow_3</bpmn:outgoing>
    </bpmn:task>
    <bpmn:endEvent id="End_1" name="End">
      <bpmn:incoming>Flow_3</bpmn:incoming>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="Flow_3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""


# Version without BPMNDiagram - matches what the LLM should produce
VALID_BPMN_XML_NO_DI = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="Process_1" isExecutable="true">
    <bpmn:startEvent id="Start_1" name="Start">
      <bpmn:outgoing>Flow_1</bpmn:outgoing>
    </bpmn:startEvent>
    <bpmn:task id="Task_1" name="Receive Application">
      <bpmn:incoming>Flow_1</bpmn:incoming>
      <bpmn:outgoing>Flow_2</bpmn:outgoing>
    </bpmn:task>
    <bpmn:task id="Task_2" name="Review Application">
      <bpmn:incoming>Flow_2</bpmn:incoming>
      <bpmn:outgoing>Flow_3</bpmn:outgoing>
    </bpmn:task>
    <bpmn:endEvent id="End_1" name="End">
      <bpmn:incoming>Flow_3</bpmn:incoming>
    </bpmn:endEvent>
    <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="Task_2"/>
    <bpmn:sequenceFlow id="Flow_3" sourceRef="Task_2" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""


# ---------------------------------------------------------------------------
# i18n helpers used by Level 2/3 tests
# ---------------------------------------------------------------------------

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Cyrillic letter ranges (covers Russian + other Cyrillic scripts)
_CYRILLIC_RANGES = [
    (0x0400, 0x04FF),  # Cyrillic
    (0x0500, 0x052F),  # Cyrillic Supplement
    (0x2DE0, 0x2DFF),  # Cyrillic Extended-A
    (0xA640, 0xA69F),  # Cyrillic Extended-B
]


def _is_cyrillic(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CYRILLIC_RANGES)


def cyrillic_ratio(s: str) -> float:
    """Share of Cyrillic letters among all letters. Non-letters ignored.

    Returns 0.0 for an empty / letter-less string.
    """
    if not s:
        return 0.0
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if _is_cyrillic(c))
    return cyr / len(letters)


def extract_flow_node_names(xml_string: str) -> list[str]:
    """Return all `name` attributes on flow nodes (non-empty only).

    Excludes sequenceFlow (which is a non-flow-node). Parses with defusedxml.
    """
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return []

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return []

    names: list[str] = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag == "sequenceFlow":
            continue
        name = elem.get("name")
        if name:
            names.append(name)
    return names


def extract_sequence_flow_names(xml_string: str) -> list[str]:
    """Return `name` attributes on sequenceFlow elements (branch labels)."""
    try:
        root = ET.fromstring(xml_string)
    except ET.ParseError:
        return []

    process = root.find(f".//{{{BPMN_NS}}}process")
    if process is None:
        process = root.find(".//process")
    if process is None:
        return []

    names: list[str] = []
    for elem in list(process):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag != "sequenceFlow":
            continue
        name = elem.get("name")
        if name:
            names.append(name)
    return names


def all_names_are_cyrillic(xml_string: str, min_ratio: float = 0.7) -> tuple[bool, list[str]]:
    """True iff every flow-node name has cyrillic_ratio >= min_ratio.

    Returns (passes, offending_names). Empty XML / no names → (False, []).
    """
    names = extract_flow_node_names(xml_string)
    if not names:
        return False, []
    offenders = [n for n in names if cyrillic_ratio(n) < min_ratio]
    return len(offenders) == 0, offenders


def all_names_are_latin(xml_string: str, max_cyr_ratio: float = 0.1) -> tuple[bool, list[str]]:
    """True iff every flow-node name has cyrillic_ratio <= max_cyr_ratio."""
    names = extract_flow_node_names(xml_string)
    if not names:
        return False, []
    offenders = [n for n in names if cyrillic_ratio(n) > max_cyr_ratio]
    return len(offenders) == 0, offenders


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_bpmn_xml():
    return VALID_BPMN_XML


@pytest.fixture
def ml_base_url() -> str:
    """Base URL of the running ML service (for E2E tests)."""
    return os.environ.get("ML_E2E_URL", "http://ml:8001")


@pytest.fixture
def backend_base_url() -> str:
    """Base URL of the running backend service (for full-stack E2E)."""
    return os.environ.get("BACKEND_E2E_URL", "http://backend:8000")


@pytest.fixture
def internal_api_key() -> str:
    """Shared secret for backend → ml calls. Tests skip if unset."""
    return os.environ.get("INTERNAL_API_KEY", "")
