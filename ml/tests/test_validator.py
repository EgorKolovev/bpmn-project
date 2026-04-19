import pytest
from app.validator import validate_bpmn_xml, get_bpmn_warnings
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

    def test_valid_with_gateway_named_flows(self):
        """Labeled branches via `name` attribute — passes validation."""
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
                <bpmn:sequenceFlow id="F2" name="Approved" sourceRef="G1" targetRef="T1"/>
                <bpmn:sequenceFlow id="F3" name="Rejected" sourceRef="G1" targetRef="T2"/>
                <bpmn:sequenceFlow id="F4" sourceRef="T1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F5" sourceRef="T2" targetRef="G2"/>
                <bpmn:sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert validate_bpmn_xml(xml) is None

    def test_valid_with_gateway_condition_expression(self):
        """Labeled branches via <conditionExpression> — passes validation."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T1"/>
                <bpmn:task id="T2"/>
                <bpmn:exclusiveGateway id="G2"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
                <bpmn:sequenceFlow id="F2" sourceRef="G1" targetRef="T1">
                    <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">approved</bpmn:conditionExpression>
                </bpmn:sequenceFlow>
                <bpmn:sequenceFlow id="F3" sourceRef="G1" targetRef="T2">
                    <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">rejected</bpmn:conditionExpression>
                </bpmn:sequenceFlow>
                <bpmn:sequenceFlow id="F4" sourceRef="T1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F5" sourceRef="T2" targetRef="G2"/>
                <bpmn:sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert validate_bpmn_xml(xml) is None

    def test_invalid_gateway_all_branches_unlabeled(self):
        """Diverging exclusiveGateway with ALL outgoing flows unlabeled — hard error."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T1"/>
                <bpmn:task id="T2"/>
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
        result = validate_bpmn_xml(xml)
        assert result is not None
        assert "G1" in result
        assert "label" in result.lower()

    def test_one_branch_labeled_ok(self):
        """Only ONE of the outgoing flows needs a label for hard-validation."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T1"/>
                <bpmn:task id="T2"/>
                <bpmn:exclusiveGateway id="G2"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
                <bpmn:sequenceFlow id="F2" name="Approved" sourceRef="G1" targetRef="T1"/>
                <bpmn:sequenceFlow id="F3" sourceRef="G1" targetRef="T2"/>
                <bpmn:sequenceFlow id="F4" sourceRef="T1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F5" sourceRef="T2" targetRef="G2"/>
                <bpmn:sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert validate_bpmn_xml(xml) is None  # Hard check passes

    def test_converging_gateway_not_checked(self):
        """exclusiveGateway with single outgoing flow (converging / pass-through)
        isn't subject to the labeling rule."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G2"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F2" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert validate_bpmn_xml(xml) is None

    def test_cycle_back_edge_valid(self):
        """Back-edge from gateway to earlier task (loop) is valid BPMN."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:task id="T1" name="Review"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T2" name="Rework"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
                <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="G1"/>
                <bpmn:sequenceFlow id="F3" name="Needs rework" sourceRef="G1" targetRef="T2"/>
                <bpmn:sequenceFlow id="F4" sourceRef="T2" targetRef="T1"/>
                <bpmn:sequenceFlow id="F5" name="Approved" sourceRef="G1" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert validate_bpmn_xml(xml) is None

    def test_empty_string(self):
        result = validate_bpmn_xml("")
        assert result is not None

    def test_plain_text(self):
        result = validate_bpmn_xml("this is not xml at all")
        assert result is not None


class TestBpmnWarnings:
    def test_no_warnings_for_linear_process(self):
        assert get_bpmn_warnings(VALID_BPMN_XML) == []

    def test_no_warnings_when_all_branches_labeled(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T1"/>
                <bpmn:task id="T2"/>
                <bpmn:exclusiveGateway id="G2"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
                <bpmn:sequenceFlow id="F2" name="A" sourceRef="G1" targetRef="T1"/>
                <bpmn:sequenceFlow id="F3" name="B" sourceRef="G1" targetRef="T2"/>
                <bpmn:sequenceFlow id="F4" sourceRef="T1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F5" sourceRef="T2" targetRef="G2"/>
                <bpmn:sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        assert get_bpmn_warnings(xml) == []

    def test_warning_when_some_branches_unlabeled(self):
        """Partial labeling → warning (but not a hard error)."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1">
            <bpmn:process id="P1">
                <bpmn:startEvent id="S1"/>
                <bpmn:exclusiveGateway id="G1"/>
                <bpmn:task id="T1"/>
                <bpmn:task id="T2"/>
                <bpmn:exclusiveGateway id="G2"/>
                <bpmn:endEvent id="E1"/>
                <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="G1"/>
                <bpmn:sequenceFlow id="F2" name="Approved" sourceRef="G1" targetRef="T1"/>
                <bpmn:sequenceFlow id="F3" sourceRef="G1" targetRef="T2"/>
                <bpmn:sequenceFlow id="F4" sourceRef="T1" targetRef="G2"/>
                <bpmn:sequenceFlow id="F5" sourceRef="T2" targetRef="G2"/>
                <bpmn:sequenceFlow id="F6" sourceRef="G2" targetRef="E1"/>
            </bpmn:process>
        </bpmn:definitions>"""
        # Hard validation still passes (at least one label present)
        assert validate_bpmn_xml(xml) is None
        # But soft check flags the unlabeled flow
        warnings = get_bpmn_warnings(xml)
        assert len(warnings) == 1
        assert "G1" in warnings[0]
        assert "F3" in warnings[0]

    def test_warnings_on_invalid_xml(self):
        # Never raises; returns [] on unparseable input.
        assert get_bpmn_warnings("<not xml") == []
        assert get_bpmn_warnings("") == []
