import pytest
from app.validator import validate_bpmn_xml
from tests.conftest import VALID_BPMN_XML


class TestValidateBpmnXml:
    def test_valid_xml(self):
        assert validate_bpmn_xml(VALID_BPMN_XML) is None

    def test_invalid_xml_syntax(self):
        result = validate_bpmn_xml("<not valid xml")
        assert result is not None
        assert "XML parse error" in result

    def test_wrong_root_element(self):
        xml = '<process xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="P1"><startEvent id="s"/><endEvent id="e"/><sequenceFlow id="f" sourceRef="s" targetRef="e"/></process>'
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "definitions" in result

    def test_no_process(self):
        xml = '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1"></definitions>'
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "process" in result.lower()

    def test_no_start_event(self):
        xml = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
            <process id="P1">
                <endEvent id="End_1"/>
            </process>
        </definitions>"""
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "startEvent" in result

    def test_no_end_event(self):
        xml = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
            <process id="P1">
                <startEvent id="Start_1"/>
            </process>
        </definitions>"""
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "endEvent" in result

    def test_sequence_flow_bad_source(self):
        xml = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
            <process id="P1">
                <startEvent id="Start_1"/>
                <endEvent id="End_1"/>
                <sequenceFlow id="Flow_1" sourceRef="NONEXISTENT" targetRef="End_1"/>
            </process>
        </definitions>"""
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "NONEXISTENT" in result

    def test_sequence_flow_bad_target(self):
        xml = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
            <process id="P1">
                <startEvent id="Start_1"/>
                <endEvent id="End_1"/>
                <sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="NONEXISTENT"/>
            </process>
        </definitions>"""
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "NONEXISTENT" in result

    def test_sequence_flow_missing_refs(self):
        xml = """<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
            <process id="P1">
                <startEvent id="Start_1"/>
                <endEvent id="End_1"/>
                <sequenceFlow id="Flow_1"/>
            </process>
        </definitions>"""
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "sourceRef" in result or "targetRef" in result

    def test_valid_with_gateway(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T1" name="Path A"/>
                <bpmn:task id="T2" name="Path B"/>
                <bpmn:exclusiveGateway id="G2"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
                <bpmn:sequenceFlow id="F2" sourceRef="G1" targetRef="T1"/>
                <bpmn:sequenceFlow id="F3" sourceRef="G1" targetRef="T2"/>
                <bpmn:sequenceFlow id="F4" sourceRef="T1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F5" sourceRef="T2" targetRef="G2"/>
                <bpmn:sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert validate_bpmn_xml(xml) is None

    def test_empty_string(self):
        result = validate_bpmn_xml("")
        assert result is not None

    def test_plain_text(self):
        result = validate_bpmn_xml("this is not xml at all")
        assert result is not None
