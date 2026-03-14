"""
Integration test for Backend Socket.IO + DB.
Run with: pytest tests/test_integration.py -v
Requires: running PostgreSQL (or uses SQLite for test)
"""
import os
import uuid
import asyncio
import pytest
import socketio as sio_client

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_integration.db"
os.environ["ML_SERVICE_URL"] = "http://localhost:8001"


VALID_BPMN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D1" targetNamespace="http://bpmn.io/schema/bpmn">
  <bpmn:process id="P1" isExecutable="true">
    <bpmn:startEvent id="S1" name="Start"/>
    <bpmn:task id="T1" name="Do Something"/>
    <bpmn:endEvent id="E1" name="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T1" targetRef="E1"/>
  </bpmn:process>
</bpmn:definitions>"""


class TestModels:
    """Test database models can be instantiated."""

    def test_session_model(self):
        from app.models import Session
        s = Session(
            user_id=uuid.uuid4(),
            name="Test Session",
            current_bpmn_xml=VALID_BPMN_XML,
        )
        assert s.name == "Test Session"
        assert s.current_bpmn_xml == VALID_BPMN_XML

    def test_message_model(self):
        from app.models import Message
        m = Message(
            session_id=uuid.uuid4(),
            role="user",
            text="Hello",
            order=0,
        )
        assert m.role == "user"
        assert m.text == "Hello"

    def test_message_assistant(self):
        from app.models import Message
        m = Message(
            session_id=uuid.uuid4(),
            role="assistant",
            bpmn_xml=VALID_BPMN_XML,
            order=1,
        )
        assert m.role == "assistant"
        assert m.bpmn_xml is not None
