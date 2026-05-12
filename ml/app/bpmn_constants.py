"""BPMN protocol constants shared by `bpmn_fix`, `bpmn_layout`, etc.

These are facts about the BPMN 2.0 specification — namespace URI and
the set of non-flow-node element tags — not runtime configuration.
They live in their own module so `app.config` stays a pure
environment-variable surface and downstream modules don't need to
import `bpmn_fix` just for a constant.
"""

# BPMN 2.0 model namespace URI (OMG spec, 2010-05-24).
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

# Element tags that are NOT flow nodes. Anything else inside a
# `<bpmn:process>` with an `id` attribute is treated as a flow node
# during post-processing.
NON_FLOW_NODE_TAGS: frozenset[str] = frozenset(
    {
        "sequenceFlow",
        "messageFlow",
        "association",
        "dataObject",
        "dataObjectReference",
        "dataStoreReference",
        "textAnnotation",
        "incoming",
        "outgoing",
        "documentation",
        "extensionElements",
        "conditionExpression",
        "multiInstanceLoopCharacteristics",
        "standardLoopCharacteristics",
        "ioSpecification",
        "dataInput",
        "dataOutput",
        "inputSet",
        "outputSet",
        "property",
        "laneSet",
        "lane",
        "flowNodeRef",
    }
)
