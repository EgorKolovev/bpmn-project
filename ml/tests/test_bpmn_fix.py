import pytest
import xml.etree.ElementTree as ET
from app.bpmn_fix import ensure_incoming_outgoing, strip_bpmn_diagram


BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"


class TestEnsureIncomingOutgoing:
    def test_adds_incoming_outgoing_to_simple_flow(self):
        """Test that incoming/outgoing refs are added to a simple linear flow."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                          id="Definitions_1"
                          targetNamespace="http://bpmn.io/schema/bpmn">
          <bpmn:process id="Process_1" isExecutable="true">
            <bpmn:startEvent id="Start_1" name="Start"/>
            <bpmn:task id="Task_1" name="Do Something"/>
            <bpmn:endEvent id="End_1" name="End"/>
            <bpmn:sequenceFlow id="Flow_1" sourceRef="Start_1" targetRef="Task_1"/>
            <bpmn:sequenceFlow id="Flow_2" sourceRef="Task_1" targetRef="End_1"/>
          </bpmn:process>
        </bpmn:definitions>"""

        result = ensure_incoming_outgoing(xml)
        root = ET.fromstring(result)
        process = root.find(f".//{{{BPMN_NS}}}process")

        # Check startEvent has outgoing=Flow_1
        start = process.find(f"{{{BPMN_NS}}}startEvent[@id='Start_1']")
        outgoing = [e.text for e in start.findall(f"{{{BPMN_NS}}}outgoing")]
        incoming = [e.text for e in start.findall(f"{{{BPMN_NS}}}incoming")]
        assert outgoing == ["Flow_1"]
        assert incoming == []

        # Check task has incoming=Flow_1, outgoing=Flow_2
        task = process.find(f"{{{BPMN_NS}}}task[@id='Task_1']")
        outgoing = [e.text for e in task.findall(f"{{{BPMN_NS}}}outgoing")]
        incoming = [e.text for e in task.findall(f"{{{BPMN_NS}}}incoming")]
        assert incoming == ["Flow_1"]
        assert outgoing == ["Flow_2"]

        # Check endEvent has incoming=Flow_2
        end = process.find(f"{{{BPMN_NS}}}endEvent[@id='End_1']")
        outgoing = [e.text for e in end.findall(f"{{{BPMN_NS}}}outgoing")]
        incoming = [e.text for e in end.findall(f"{{{BPMN_NS}}}incoming")]
        assert incoming == ["Flow_2"]
        assert outgoing == []

    def test_handles_gateway_with_multiple_paths(self):
        """Test gateways with diverging/converging flows get correct refs."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                          id="D1" targetNamespace="http://bpmn.io/schema/bpmn">
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

        result = ensure_incoming_outgoing(xml)
        root = ET.fromstring(result)
        process = root.find(f".//{{{BPMN_NS}}}process")

        # Diverging gateway G1 should have incoming=F1, outgoing=[F2, F3]
        g1 = process.find(f"{{{BPMN_NS}}}exclusiveGateway[@id='G1']")
        g1_in = [e.text for e in g1.findall(f"{{{BPMN_NS}}}incoming")]
        g1_out = [e.text for e in g1.findall(f"{{{BPMN_NS}}}outgoing")]
        assert g1_in == ["F1"]
        assert sorted(g1_out) == ["F2", "F3"]

        # Converging gateway G2 should have incoming=[F4, F5], outgoing=F6
        g2 = process.find(f"{{{BPMN_NS}}}exclusiveGateway[@id='G2']")
        g2_in = [e.text for e in g2.findall(f"{{{BPMN_NS}}}incoming")]
        g2_out = [e.text for e in g2.findall(f"{{{BPMN_NS}}}outgoing")]
        assert sorted(g2_in) == ["F4", "F5"]
        assert g2_out == ["F6"]

    def test_replaces_existing_incorrect_refs(self):
        """Test that existing wrong incoming/outgoing elements are replaced."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                          id="D1" targetNamespace="http://bpmn.io/schema/bpmn">
          <bpmn:process id="P1">
            <bpmn:startEvent id="S1">
              <bpmn:outgoing>WRONG_FLOW</bpmn:outgoing>
            </bpmn:startEvent>
            <bpmn:endEvent id="E1">
              <bpmn:incoming>WRONG_FLOW</bpmn:incoming>
            </bpmn:endEvent>
            <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="E1"/>
          </bpmn:process>
        </bpmn:definitions>"""

        result = ensure_incoming_outgoing(xml)
        root = ET.fromstring(result)
        process = root.find(f".//{{{BPMN_NS}}}process")

        start = process.find(f"{{{BPMN_NS}}}startEvent[@id='S1']")
        outgoing = [e.text for e in start.findall(f"{{{BPMN_NS}}}outgoing")]
        assert outgoing == ["F1"]  # Corrected from WRONG_FLOW

        end = process.find(f"{{{BPMN_NS}}}endEvent[@id='E1']")
        incoming = [e.text for e in end.findall(f"{{{BPMN_NS}}}incoming")]
        assert incoming == ["F1"]

    def test_idempotent(self):
        """Running ensure_incoming_outgoing twice produces the same result."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                          id="D1" targetNamespace="http://bpmn.io/schema/bpmn">
          <bpmn:process id="P1">
            <bpmn:startEvent id="S1"/>
            <bpmn:task id="T1" name="Do"/>
            <bpmn:endEvent id="E1"/>
            <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
            <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
          </bpmn:process>
        </bpmn:definitions>"""

        result1 = ensure_incoming_outgoing(xml)
        result2 = ensure_incoming_outgoing(result1)

        # Parse both and compare structure
        root1 = ET.fromstring(result1)
        root2 = ET.fromstring(result2)

        process1 = root1.find(f".//{{{BPMN_NS}}}process")
        process2 = root2.find(f".//{{{BPMN_NS}}}process")

        # Same number of incoming/outgoing elements
        for node_id in ["S1", "T1", "E1"]:
            for tag in ["startEvent", "task", "endEvent"]:
                n1 = process1.find(f".//{{{BPMN_NS}}}{tag}[@id='{node_id}']")
                n2 = process2.find(f".//{{{BPMN_NS}}}{tag}[@id='{node_id}']")
                if n1 is not None and n2 is not None:
                    in1 = [e.text for e in n1.findall(f"{{{BPMN_NS}}}incoming")]
                    in2 = [e.text for e in n2.findall(f"{{{BPMN_NS}}}incoming")]
                    out1 = [e.text for e in n1.findall(f"{{{BPMN_NS}}}outgoing")]
                    out2 = [e.text for e in n2.findall(f"{{{BPMN_NS}}}outgoing")]
                    assert in1 == in2
                    assert out1 == out2

    def test_invalid_xml_returns_unchanged(self):
        """Invalid XML should be returned as-is."""
        bad_xml = "<not valid xml"
        result = ensure_incoming_outgoing(bad_xml)
        assert result == bad_xml

    def test_no_process_returns_unchanged(self):
        """XML without a process element returns unchanged."""
        xml = '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1"></bpmn:definitions>'
        result = ensure_incoming_outgoing(xml)
        # Should still be valid XML, just unchanged process-wise
        assert "definitions" in result


class TestStripBpmnDiagram:
    def test_strips_diagram_section(self):
        xml = """<?xml version="1.0"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                          xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI">
          <bpmn:process id="P1">
            <bpmn:startEvent id="S1"/>
          </bpmn:process>
          <bpmndi:BPMNDiagram id="D1">
            <bpmndi:BPMNPlane id="P1_di" bpmnElement="P1"/>
          </bpmndi:BPMNDiagram>
        </bpmn:definitions>"""

        result = strip_bpmn_diagram(xml)
        assert "BPMNDiagram" not in result
        assert "startEvent" in result

    def test_no_diagram_unchanged(self):
        xml = """<?xml version="1.0"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
          <bpmn:process id="P1">
            <bpmn:startEvent id="S1"/>
          </bpmn:process>
        </bpmn:definitions>"""

        result = strip_bpmn_diagram(xml)
        assert "startEvent" in result
