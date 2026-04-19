"""Unit tests for lane (swimlane) support — validator + bpmn_fix."""
from app.bpmn_fix import ensure_lane_refs
from app.validator import validate_bpmn_xml


LANE_XML_VALID = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  id="D1" targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="P1">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="Lane_Manager" name="Менеджер">
        <bpmn:flowNodeRef>S1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>T1</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="Lane_Director" name="Директор">
        <bpmn:flowNodeRef>T2</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>E1</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="S1"/>
    <bpmn:task id="T1" name="Создать заявку"/>
    <bpmn:task id="T2" name="Утверждение"/>
    <bpmn:endEvent id="E1"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="T2"/>
    <bpmn:sequenceFlow id="F3" sourceRef="T2" targetRef="E1"/>
  </bpmn:process>
</bpmn:definitions>"""


LANE_XML_DUPLICATE = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="D1">
  <bpmn:process id="P1">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="L1" name="A">
        <bpmn:flowNodeRef>T1</bpmn:flowNodeRef>
      </bpmn:lane>
      <bpmn:lane id="L2" name="B">
        <bpmn:flowNodeRef>T1</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="S1"/>
    <bpmn:task id="T1"/>
    <bpmn:endEvent id="E1"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
  </bpmn:process>
</bpmn:definitions>"""


LANE_XML_MISSING_REF = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="D1">
  <bpmn:process id="P1">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="L1" name="A">
        <bpmn:flowNodeRef>S1</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="S1"/>
    <bpmn:task id="T1"/>
    <bpmn:endEvent id="E1"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
  </bpmn:process>
</bpmn:definitions>"""


LANE_XML_STRAY_REF = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  id="D1">
  <bpmn:process id="P1">
    <bpmn:laneSet id="LaneSet_1">
      <bpmn:lane id="L1" name="A">
        <bpmn:flowNodeRef>S1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>T1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>E1</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>NONEXISTENT</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
    <bpmn:startEvent id="S1"/>
    <bpmn:task id="T1"/>
    <bpmn:endEvent id="E1"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
  </bpmn:process>
</bpmn:definitions>"""


class TestValidatorLaneRules:
    def test_valid_lanes(self):
        assert validate_bpmn_xml(LANE_XML_VALID) is None

    def test_duplicate_lane_assignment_rejected(self):
        result = validate_bpmn_xml(LANE_XML_DUPLICATE)
        assert result is not None
        assert "T1" in result
        assert "lane" in result.lower()

    def test_stray_flow_node_ref_rejected(self):
        result = validate_bpmn_xml(LANE_XML_STRAY_REF)
        assert result is not None
        assert "NONEXISTENT" in result

    def test_missing_ref_validator_doesnt_block(self):
        """Missing refs are soft-fixed by ensure_lane_refs, not a hard error."""
        # Validator passes because missing is considered recoverable
        assert validate_bpmn_xml(LANE_XML_MISSING_REF) is None


class TestEnsureLaneRefs:
    def _parse_lanes(self, xml: str) -> dict[str, list[str]]:
        """Return {lane_id: [flow_node_ids]}."""
        from defusedxml import ElementTree as ET
        NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
        root = ET.fromstring(xml)
        process = root.find(f".//{{{NS}}}process")
        lane_set = process.find(f"{{{NS}}}laneSet")
        if lane_set is None:
            return {}
        out: dict[str, list[str]] = {}
        for lane in lane_set.findall(f"{{{NS}}}lane"):
            ids = [
                (c.text or "").strip()
                for c in lane.findall(f"{{{NS}}}flowNodeRef")
            ]
            out[lane.get("id", "")] = ids
        return out

    def test_no_laneset_returns_unchanged(self):
        xml = """<?xml version="1.0"?>
        <bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">
          <bpmn:process id="P1">
            <bpmn:startEvent id="S1"/>
            <bpmn:endEvent id="E1"/>
            <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="E1"/>
          </bpmn:process>
        </bpmn:definitions>"""
        assert ensure_lane_refs(xml) == xml

    def test_already_correct_is_preserved(self):
        result = ensure_lane_refs(LANE_XML_VALID)
        lanes = self._parse_lanes(result)
        assert lanes["Lane_Manager"] == ["S1", "T1"]
        assert lanes["Lane_Director"] == ["T2", "E1"]

    def test_duplicate_assignment_is_deduped(self):
        result = ensure_lane_refs(LANE_XML_DUPLICATE)
        lanes = self._parse_lanes(result)
        # T1 must appear in exactly ONE lane now (the first one that claimed it)
        total = sum(lane.count("T1") for lane in lanes.values())
        assert total == 1
        # L1 (first lane) got T1
        assert "T1" in lanes["L1"]
        assert "T1" not in lanes["L2"]

    def test_missing_nodes_added_to_first_lane(self):
        """LANE_XML_MISSING_REF has only S1 in L1, but T1 and E1 also exist
        as flow nodes. They must be added to L1 (the only lane)."""
        result = ensure_lane_refs(LANE_XML_MISSING_REF)
        lanes = self._parse_lanes(result)
        assert set(lanes["L1"]) == {"S1", "T1", "E1"}

    def test_stray_refs_dropped(self):
        result = ensure_lane_refs(LANE_XML_STRAY_REF)
        lanes = self._parse_lanes(result)
        # NONEXISTENT should no longer appear
        assert "NONEXISTENT" not in lanes["L1"]
        assert set(lanes["L1"]) == {"S1", "T1", "E1"}

    def test_idempotent(self):
        once = ensure_lane_refs(LANE_XML_VALID)
        twice = ensure_lane_refs(once)
        assert self._parse_lanes(once) == self._parse_lanes(twice)

    def test_invalid_xml_returns_unchanged(self):
        bad = "<not xml"
        assert ensure_lane_refs(bad) == bad
