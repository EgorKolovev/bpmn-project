"""Backend integration tests (Layer 2A/2B) — exercise `handle_message`
and `handle_action` against:

  * A real (sqlite-aiosqlite) database that gets created per-test.
  * A monkey-patched `_FakeSio` that captures every emit/save_session
    call so we can assert events without spinning up a Socket.IO server.
  * `mock_ml` (from `conftest.py`) for the ml-service HTTP boundary —
    every test arranges canned classify/generate/edit responses.

The tests cover the contract surface clients depend on:

  * New session vs. existing session → /generate vs /edit routing.
  * BPMN XML + session_name round-trip.
  * Classify rejection → error event with reason.
  * Classify HTTP error → silently let through (fallback).
  * ML 500 / 429 / timeout → user gets a friendly error event,
    process state is not corrupted.
  * Rate limit per SID (5 messages / 10 s).
  * `_normalize_message_text` validation (1-char, empty, oversized).
  * Bound user_id mismatch.
  * Unknown action.
  * Database unavailable → generic "Internal server error", no leaks.
  * Session reconnect with valid / invalid / expired session token.
"""

import os
import uuid

import httpx
import pytest
import pytest_asyncio

# Env is set in `conftest.py` (DATABASE_URL forced to sqlite, etc.).
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")


# ---------------------------------------------------------------------------
# Fake Socket.IO server — captures every emit/session call so the test
# can assert directly on what would have gone out over the wire.
# ---------------------------------------------------------------------------


class _FakeSio:
    def __init__(self):
        self.emitted: list[dict] = []
        self._sessions: dict[str, dict] = {}

    async def emit(self, event, data, to=None):
        self.emitted.append({"event": event, "data": data, "to": to})

    async def save_session(self, sid, payload):
        self._sessions.setdefault(sid, {}).update(payload)

    async def get_session(self, sid):
        return dict(self._sessions.get(sid, {}))

    # Helpers for assertion sugar.
    def emits_of_action(self, action: str) -> list[dict]:
        out = []
        for ev in self.emitted:
            data = ev.get("data") or {}
            if data.get("action") == action:
                out.append(data)
        return out

    def first(self, action: str) -> dict:
        m = self.emits_of_action(action)
        assert m, f"no emits with action={action!r}; saw: {[e['data'].get('action') for e in self.emitted]}"
        return m[0]

    def clear(self) -> None:
        self.emitted.clear()


@pytest_asyncio.fixture
async def fake_sio(monkeypatch):
    from app import main as backend_main

    fake = _FakeSio()
    monkeypatch.setattr(backend_main, "sio", fake)
    yield fake


@pytest_asyncio.fixture(autouse=True)
async def init_db_per_test():
    """Recreate the engine + tables before each test so SQLAlchemy's
    connection pool can't leak across event loops.

    `app.database.engine` is module-level and binds to the loop of its
    first request. Without per-test disposal, the 2nd+ test gets
    cached connections pinned to a closed loop — silent failures
    surface as missing tables or hanging transactions.
    """
    from app import database as db_module

    # Throw away any pool state from the previous test.
    try:
        await db_module.engine.dispose()
    except Exception:
        pass
    # Rebuild engine + sessionmaker against the current loop.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    pg_kwargs = (
        {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}
        if db_module.DATABASE_URL.startswith("postgresql")
        else {}
    )
    db_module.engine = create_async_engine(db_module.DATABASE_URL, echo=False, **pg_kwargs)
    db_module.async_session = async_sessionmaker(
        db_module.engine, class_=AsyncSession, expire_on_commit=False
    )
    # Also patch the alias inside app.main (`from app.database import async_session`).
    from app import main as backend_main

    backend_main.async_session = db_module.async_session

    await db_module.init_db()
    yield
    await db_module.engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def init_session_secret(monkeypatch):
    """`_resolve_user_identity` needs a non-None signing secret. The
    real startup hook does this; here we just patch a fixed value."""
    from app import main as backend_main

    monkeypatch.setattr(backend_main, "session_signing_secret", "test-signing-secret")
    yield


@pytest_asyncio.fixture(autouse=True)
async def reset_rate_limit():
    """`_check_rate_limit` keeps in-process state in a module-level
    `_rate_limit_map`. Wipe it between tests so order doesn't matter."""
    from app import main as backend_main

    backend_main._rate_limit_map.clear()
    yield
    backend_main._rate_limit_map.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SAMPLE_BPMN_XML = """<?xml version='1.0' encoding='UTF-8'?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI">
  <bpmn:process id="P_1" isExecutable="true">
    <bpmn:startEvent id="S_1"><bpmn:outgoing>F1</bpmn:outgoing></bpmn:startEvent>
    <bpmn:task id="T_1" name="Do Thing"><bpmn:incoming>F1</bpmn:incoming><bpmn:outgoing>F2</bpmn:outgoing></bpmn:task>
    <bpmn:endEvent id="E_1"><bpmn:incoming>F2</bpmn:incoming></bpmn:endEvent>
    <bpmn:sequenceFlow id="F1" sourceRef="S_1" targetRef="T_1"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T_1" targetRef="E_1"/>
  </bpmn:process>
  <bpmndi:BPMNDiagram id="D1"/>
</bpmn:definitions>"""


async def _seed_init(sid: str, fake_sio: _FakeSio, *, mock_ml=None) -> str:
    """Run the `init` handshake so the SID has a bound user_id, then
    return that user_id as a string."""
    from app.main import handle_action

    await handle_action(sid, {"action": "init"})
    init_data = fake_sio.first("init_data")
    return init_data["user_id"]


def _ok_classify() -> dict:
    return {"is_valid": True}


def _reject_classify(reason: str = "Not a process") -> dict:
    return {"is_valid": False, "reason": reason}


def _ok_generate(name: str = "Test Process", xml: str = SAMPLE_BPMN_XML) -> dict:
    return {"bpmn_xml": xml, "session_name": name}


def _ok_edit(xml: str = SAMPLE_BPMN_XML) -> dict:
    return {"bpmn_xml": xml}


# ---------------------------------------------------------------------------
# Routing: new session → /generate, existing session → /edit
# ---------------------------------------------------------------------------


class TestRouting:
    async def test_new_session_calls_generate(self, fake_sio, mock_ml):
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/generate": _ok_generate("Sample"),
            }
        )
        await _seed_init("sid-1", fake_sio)
        from app.main import handle_action

        fake_sio.clear()

        await handle_action("sid-1", {"action": "message", "text": "Onboarding process."})

        # generate was called, edit was not
        paths = [r.url.path for r in mock_ml.requests]
        assert "/generate" in paths
        assert "/edit" not in paths

        # result event went out with the canned bpmn_xml + session_name
        result = fake_sio.first("result")
        assert result["session_name"] == "Sample"
        assert "<bpmn:process" in result["bpmn_xml"]
        assert "session_id" in result

    async def test_existing_session_calls_edit(self, fake_sio, mock_ml):
        # First create a session via /generate.
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/generate": _ok_generate("Original"),
            }
        )
        await _seed_init("sid-2", fake_sio)
        from app.main import handle_action

        await handle_action("sid-2", {"action": "message", "text": "Process v1"})

        result1 = fake_sio.first("result")
        session_id = result1["session_id"]

        # Now switch responses and send a second message with the existing session_id.
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/edit": _ok_edit(),
            }
        )
        fake_sio.clear()
        mock_ml.clear_requests()
        await handle_action(
            "sid-2",
            {"action": "message", "text": "Add a step.", "session_id": session_id},
        )

        paths = [r.url.path for r in mock_ml.requests]
        assert "/edit" in paths
        assert "/generate" not in paths

        # The /edit body must include the current bpmn_xml from the DB
        # (so the LLM can apply the patch). Inspect the captured body.
        edit_bodies = [
            b
            for r, b in zip(mock_ml.requests, mock_ml.request_bodies, strict=False)
            if r.url.path == "/edit"
        ]
        assert edit_bodies, "no /edit body captured"
        body = edit_bodies[0]
        assert body.get("prompt") == "Add a step."
        assert "<bpmn:process" in body.get("bpmn_xml", "")

    async def test_edit_updates_session_current_bpmn(self, fake_sio, mock_ml):
        # Bootstrap initial session.
        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await _seed_init("sid-3", fake_sio)
        from app.main import handle_action

        await handle_action("sid-3", {"action": "message", "text": "Process v1"})
        session_id = fake_sio.first("result")["session_id"]

        # Edit produces a DIFFERENT XML.
        updated_xml = SAMPLE_BPMN_XML.replace("Do Thing", "Updated Thing")
        mock_ml.set({"/classify": _ok_classify(), "/edit": _ok_edit(updated_xml)})
        fake_sio.clear()
        await handle_action(
            "sid-3",
            {"action": "message", "text": "rename", "session_id": session_id},
        )

        result = fake_sio.first("result")
        assert "Updated Thing" in result["bpmn_xml"]

        # And the DB should reflect the new current_bpmn_xml.
        from sqlalchemy import select

        from app.database import async_session
        from app.models import Session as DbSession

        async with async_session() as db:
            row = await db.execute(select(DbSession).where(DbSession.id == uuid.UUID(session_id)))
            stored = row.scalar_one()
            assert "Updated Thing" in stored.current_bpmn_xml


# ---------------------------------------------------------------------------
# Classification — rejection and fallback
# ---------------------------------------------------------------------------


class TestClassify:
    async def test_classify_rejection_emits_error(self, fake_sio, mock_ml):
        mock_ml.set(
            {
                "/classify": _reject_classify("Looks like weather."),
            }
        )
        await _seed_init("sid-c1", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action(
            "sid-c1",
            {"action": "message", "text": "Какая сегодня погода?"},
        )

        # Generate was NEVER called.
        assert all(r.url.path != "/generate" for r in mock_ml.requests)

        # An error event went out, carrying the reason.
        errors = fake_sio.emits_of_action("error")
        assert errors, f"expected error emit; saw: {fake_sio.emitted}"
        msg = errors[0].get("message", "").lower()
        assert "weather" in msg or "process" in msg

    async def test_classify_500_falls_through(self, fake_sio, mock_ml):
        """ML /classify is unavailable → handler logs but lets the request
        proceed to /generate. This is the documented fallback so a flaky
        classifier doesn't block legitimate users."""
        mock_ml.set(
            {
                "/classify": (500, {"detail": "boom"}),
                "/generate": _ok_generate("Still works"),
            }
        )
        await _seed_init("sid-c2", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action(
            "sid-c2",
            {"action": "message", "text": "Process description"},
        )

        # /generate was called despite classify failing.
        assert any(r.url.path == "/generate" for r in mock_ml.requests)
        # Result was emitted, not an error.
        result = fake_sio.first("result")
        assert result["session_name"] == "Still works"

    async def test_classify_network_error_falls_through(self, fake_sio, mock_ml):
        """Same fallback path, but the error is a connection-level
        `httpx.RequestError` rather than an HTTP status."""
        mock_ml.set(
            {
                "/classify": httpx.ConnectError("network down"),
                "/generate": _ok_generate("Still works"),
            }
        )
        await _seed_init("sid-c3", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action(
            "sid-c3",
            {"action": "message", "text": "Process"},
        )

        # /generate was called.
        assert any(r.url.path == "/generate" for r in mock_ml.requests)
        result = fake_sio.first("result")
        assert result["session_name"] == "Still works"


# ---------------------------------------------------------------------------
# ML errors → user-friendly emit
# ---------------------------------------------------------------------------


class TestMLErrors:
    async def test_ml_500_on_generate_emits_friendly_error(self, fake_sio, mock_ml):
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/generate": (500, {"detail": "boom"}),
            }
        )
        await _seed_init("sid-e1", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action("sid-e1", {"action": "message", "text": "Process"})

        errors = fake_sio.emits_of_action("error")
        assert errors
        # Generic message — must NOT leak ml internals.
        assert "boom" not in errors[0]["message"]
        assert errors[0]["message"]

    async def test_ml_429_signals_rate_limit_to_user(self, fake_sio, mock_ml):
        """ML 429 (daily budget cap) should surface a 'try again later'
        message rather than a generic 'processing failed'."""
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/generate": (429, {"detail": "Daily cap reached"}),
            }
        )
        await _seed_init("sid-e2", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action("sid-e2", {"action": "message", "text": "Process"})

        errors = fake_sio.emits_of_action("error")
        assert errors
        assert "later" in errors[0]["message"].lower() or "cap" in errors[0]["message"].lower()

    async def test_ml_network_error_emits_friendly_error(self, fake_sio, mock_ml):
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/generate": httpx.ConnectTimeout("upstream timed out"),
            }
        )
        await _seed_init("sid-e3", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action("sid-e3", {"action": "message", "text": "Process"})

        errors = fake_sio.emits_of_action("error")
        assert errors
        assert "timed out" not in errors[0]["message"]  # no leak

    async def test_ml_returns_empty_bpmn_xml_emits_error(self, fake_sio, mock_ml):
        """Defensive: if ml ever returns `{"bpmn_xml": ""}` we don't
        write a broken session to the DB; we surface an error."""
        mock_ml.set(
            {
                "/classify": _ok_classify(),
                "/generate": {"bpmn_xml": "", "session_name": "Empty"},
            }
        )
        await _seed_init("sid-e4", fake_sio)
        fake_sio.clear()
        from app.main import handle_action

        await handle_action("sid-e4", {"action": "message", "text": "Process"})

        errors = fake_sio.emits_of_action("error")
        assert errors


# ---------------------------------------------------------------------------
# Rate limiting per SID — 5 messages / 10 s window
# ---------------------------------------------------------------------------


class TestRateLimit:
    async def test_rate_limit_blocks_after_threshold(self, fake_sio, mock_ml):
        from app.main import RATE_LIMIT_MAX, handle_action

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await _seed_init("sid-rl", fake_sio)
        fake_sio.clear()

        for i in range(RATE_LIMIT_MAX):
            await handle_action(
                "sid-rl",
                {"action": "message", "text": f"req {i}"},
            )

        # The (RATE_LIMIT_MAX + 1)-th call should be rejected.
        fake_sio.clear()
        await handle_action(
            "sid-rl",
            {"action": "message", "text": "one too many"},
        )
        errors = fake_sio.emits_of_action("error")
        assert errors, "expected rate-limit error emit"
        assert "many" in errors[0]["message"].lower() or "wait" in errors[0]["message"].lower()

    async def test_rate_limit_isolated_per_sid(self, fake_sio, mock_ml):
        from app.main import RATE_LIMIT_MAX, handle_action

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await _seed_init("sid-rl-a", fake_sio)
        await _seed_init("sid-rl-b", fake_sio)
        fake_sio.clear()

        # Exhaust sid-rl-a.
        for i in range(RATE_LIMIT_MAX):
            await handle_action("sid-rl-a", {"action": "message", "text": f"a{i}"})

        # sid-rl-b should still go through on its first call.
        fake_sio.clear()
        await handle_action("sid-rl-b", {"action": "message", "text": "b0"})
        assert fake_sio.emits_of_action("result"), "rate-limit state leaked between SIDs"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    async def test_empty_text_rejected(self, fake_sio, mock_ml):
        from app.main import handle_action

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await _seed_init("sid-iv1", fake_sio)
        fake_sio.clear()

        await handle_action("sid-iv1", {"action": "message", "text": ""})
        errors = fake_sio.emits_of_action("error")
        assert errors
        # ml should NEVER be called for empty input.
        assert not mock_ml.requests

    async def test_oversized_text_rejected(self, fake_sio, mock_ml):
        from app.main import MAX_MESSAGE_CHARS, handle_action

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await _seed_init("sid-iv2", fake_sio)
        fake_sio.clear()

        await handle_action(
            "sid-iv2",
            {"action": "message", "text": "x" * (MAX_MESSAGE_CHARS + 1)},
        )
        errors = fake_sio.emits_of_action("error")
        assert errors
        assert "exceed" in errors[0]["message"].lower() or "long" in errors[0]["message"].lower()
        assert not mock_ml.requests

    async def test_non_string_text_rejected(self, fake_sio, mock_ml):
        from app.main import handle_action

        await _seed_init("sid-iv3", fake_sio)
        fake_sio.clear()

        await handle_action("sid-iv3", {"action": "message", "text": 12345})
        errors = fake_sio.emits_of_action("error")
        assert errors

    async def test_unknown_action_rejected(self, fake_sio, mock_ml):
        from app.main import handle_action

        await _seed_init("sid-iv4", fake_sio)
        fake_sio.clear()

        await handle_action("sid-iv4", {"action": "do_evil"})
        errors = fake_sio.emits_of_action("error")
        assert errors
        assert "unknown" in errors[0]["message"].lower()

    async def test_invalid_payload_shape_rejected(self, fake_sio, mock_ml):
        """payload is a string instead of a dict — must not crash."""
        from app.main import handle_action

        await handle_action("sid-iv5", "not a dict")
        errors = fake_sio.emits_of_action("error")
        assert errors


# ---------------------------------------------------------------------------
# Session ownership — a user can't edit someone else's session.
# ---------------------------------------------------------------------------


class TestSessionOwnership:
    async def test_edit_other_users_session_rejected(self, fake_sio, mock_ml):
        from app.main import handle_action

        # User A creates a session.
        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate("A's")})
        await _seed_init("sid-a", fake_sio)
        await handle_action("sid-a", {"action": "message", "text": "Process"})
        result_a = fake_sio.first("result")
        session_id_a = result_a["session_id"]

        # User B (different SID, no shared user_id) tries to edit.
        fake_sio._sessions.clear()
        fake_sio.clear()
        mock_ml.clear_requests()
        await _seed_init("sid-b", fake_sio)
        fake_sio.clear()

        await handle_action(
            "sid-b",
            {"action": "message", "text": "hack", "session_id": session_id_a},
        )

        errors = fake_sio.emits_of_action("error")
        assert errors
        assert "session not found" in errors[0]["message"].lower()
        # ml /edit must NOT have been called for the foreign session.
        assert not any(r.url.path == "/edit" for r in mock_ml.requests)


# ---------------------------------------------------------------------------
# Database unavailable — defensive layer
#
# Failure of the underlying DB must NOT
#   * crash the worker,
#   * leak SQL/driver internals to the client,
#   * leave the user with an unhandled exception event.
#
# Instead, the top-level `except Exception` in `handle_action` catches
# any non-`ClientInputError` and emits a generic "Internal server error.".
#
# We simulate the failure by monkey-patching `app.main.async_session`
# to a context manager that raises `sqlalchemy.exc.OperationalError`
# on entry — that's the same exception family `asyncpg` raises when
# the Postgres connection drops, so the test exercises the same
# real-world code path without needing to spin up / kill a DB container.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def failing_db(monkeypatch):
    """Replace `app.main.async_session` with a factory that immediately
    raises `OperationalError`. This is the cheapest way to simulate a
    dropped DB connection — the engine itself stays healthy so the
    autouse `init_db_per_test` teardown still works."""
    from sqlalchemy.exc import OperationalError

    from app import main as backend_main

    class _FailingSession:
        async def __aenter__(self):
            raise OperationalError(
                "SELECT 1",
                None,
                Exception("connection refused"),
            )

        async def __aexit__(self, *exc):
            return False

    def _fail_factory():
        return _FailingSession()

    monkeypatch.setattr(backend_main, "async_session", _fail_factory)
    yield


class TestDatabaseUnavailable:
    async def test_init_with_db_down_emits_internal_error(self, fake_sio, failing_db):
        """`handle_init` queries the DB for the user's prior sessions.
        With the DB unavailable, no `init_data` event should go out;
        the client should see a generic error instead."""
        from app.main import handle_action

        await handle_action("sid-db1", {"action": "init"})

        assert not fake_sio.emits_of_action("init_data")
        errors = fake_sio.emits_of_action("error")
        assert errors, f"expected error emit; saw: {fake_sio.emitted}"
        msg = errors[0]["message"]
        assert msg == "Internal server error."
        # Driver internals MUST NOT leak.
        lower = msg.lower()
        for needle in ("connection refused", "operationalerror", "select 1", "sqlalchemy"):
            assert needle not in lower, f"DB internals leaked into client message: {msg!r}"

    async def test_message_new_session_with_db_down_emits_internal_error(
        self, fake_sio, mock_ml, failing_db
    ):
        """`handle_message` (new-session path) reaches `async with
        async_session()` AFTER classify+generate already succeeded.
        The DB failure must still surface as a generic error and not
        as e.g. a half-emitted `result` event."""
        from app.main import handle_action

        # Seed the sio session directly — we can't call _seed_init through
        # `failing_db` since init itself would fail.
        user_id = str(uuid.uuid4())
        await fake_sio.save_session("sid-db2", {"user_id": user_id})

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})

        await handle_action("sid-db2", {"action": "message", "text": "process description"})

        # No `result` — we never got past the DB write.
        assert not fake_sio.emits_of_action("result")
        errors = fake_sio.emits_of_action("error")
        assert errors
        assert errors[0]["message"] == "Internal server error."

    async def test_message_edit_with_db_down_emits_internal_error(
        self, fake_sio, mock_ml, failing_db
    ):
        """Edit path: DB fails before we even read the existing
        `current_bpmn_xml`. No /edit call to ml must be made."""
        from app.main import handle_action

        user_id = str(uuid.uuid4())
        await fake_sio.save_session("sid-db3", {"user_id": user_id})

        mock_ml.set({"/classify": _ok_classify(), "/edit": _ok_edit()})

        await handle_action(
            "sid-db3",
            {
                "action": "message",
                "text": "rename step",
                "session_id": str(uuid.uuid4()),
            },
        )

        # We never reached the /edit call.
        assert not any(r.url.path == "/edit" for r in mock_ml.requests)
        errors = fake_sio.emits_of_action("error")
        assert errors
        assert errors[0]["message"] == "Internal server error."

    async def test_open_session_with_db_down_emits_internal_error(self, fake_sio, failing_db):
        from app.main import handle_action

        user_id = str(uuid.uuid4())
        await fake_sio.save_session("sid-db4", {"user_id": user_id})

        await handle_action(
            "sid-db4",
            {"action": "open_session", "session_id": str(uuid.uuid4())},
        )

        errors = fake_sio.emits_of_action("error")
        assert errors
        assert errors[0]["message"] == "Internal server error."

    async def test_db_failure_does_not_corrupt_subsequent_requests(
        self, fake_sio, mock_ml, monkeypatch
    ):
        """One request hits a DB outage; the very next request should
        succeed normally. Verifies the failure path doesn't leave a
        dangling transaction / locked sessionmaker behind."""
        # First request — DB is broken.
        from sqlalchemy.exc import OperationalError

        from app import main as backend_main
        from app.main import handle_action

        class _Boom:
            async def __aenter__(self):
                raise OperationalError("x", None, Exception("down"))

            async def __aexit__(self, *exc):
                return False

        original_session = backend_main.async_session
        monkeypatch.setattr(backend_main, "async_session", lambda: _Boom())
        await handle_action("sid-db5", {"action": "init"})
        assert fake_sio.emits_of_action("error")
        fake_sio.clear()

        # Restore — DB is healthy again.
        monkeypatch.setattr(backend_main, "async_session", original_session)
        await handle_action("sid-db5", {"action": "init"})

        # init_data must come through cleanly.
        init_data = fake_sio.first("init_data")
        assert "user_id" in init_data


# ---------------------------------------------------------------------------
# Session reconnect — page-refresh / wifi-blip continuity
#
# Contract:
#   * On first connect, the server issues `user_id` + `session_token`
#     and the client persists them locally (frontend keeps them in
#     localStorage).
#   * On reconnect, the client supplies (user_id, session_token) via the
#     Socket.IO handshake's `auth` payload.
#   * The server verifies the HMAC signature in `_resolve_user_identity`
#     and — if valid — emits `init_data` with the *same* user_id, so
#     the user's session list survives the disconnect.
#   * An invalid / expired token must produce a *fresh* identity, never
#     trust the claimed user_id.
#
# `connect` and `disconnect` are the real Socket.IO server handlers
# (decorated with `@sio.event`); we invoke them directly. The module
# globals `sio` and `async_session` are already patched by the autouse
# fixtures above.
# ---------------------------------------------------------------------------


class TestSessionReconnect:
    async def test_disconnect_clears_rate_limit_state(self, fake_sio, mock_ml):
        """`disconnect` must remove this SID from `_rate_limit_map`,
        otherwise a long-running server would accumulate dead counters
        and eventually run out of memory."""
        from app.main import (
            RATE_LIMIT_MAX,
            _rate_limit_map,
            disconnect,
            handle_action,
        )

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await _seed_init("sid-rec1", fake_sio)

        for i in range(RATE_LIMIT_MAX):
            await handle_action("sid-rec1", {"action": "message", "text": f"req {i}"})

        assert "sid-rec1" in _rate_limit_map
        assert _rate_limit_map[
            "sid-rec1"
        ], "rate-limit map should have at least one timestamp before disconnect"

        await disconnect("sid-rec1")

        assert (
            "sid-rec1" not in _rate_limit_map
        ), "disconnect must wipe the rate-limit counter for this SID"

    async def test_reconnect_with_valid_token_restores_user_id(self, fake_sio, mock_ml):
        """Real reconnect flow: first init issues (user_id, token);
        a fresh SID that connects with that same (user_id, token)
        must get the original user_id back on init."""
        from app.main import connect, disconnect, handle_action

        # First connect + init — token issued.
        await connect("sid-orig", {}, None)
        await handle_action("sid-orig", {"action": "init"})
        first_init = fake_sio.first("init_data")
        original_user_id = first_init["user_id"]
        session_token = first_init["session_token"]
        assert original_user_id and session_token

        await disconnect("sid-orig")
        fake_sio.clear()

        # New SID carrying the stored credentials.
        await connect(
            "sid-back",
            {},
            {"user_id": original_user_id, "session_token": session_token},
        )
        await handle_action("sid-back", {"action": "init"})

        second_init = fake_sio.first("init_data")
        assert second_init["user_id"] == original_user_id, (
            f"reconnect changed user_id: {original_user_id} → " f"{second_init['user_id']}"
        )
        assert second_init["session_token"] == session_token

    async def test_reconnect_with_invalid_token_creates_fresh_identity(self, fake_sio, mock_ml):
        """Security contract: if the token signature is wrong, the
        server must NOT trust the claimed user_id — otherwise anyone
        who guesses a victim's user_id could impersonate them."""
        from app.main import connect, handle_action

        # First valid init to learn a real user_id we can try to spoof.
        await connect("sid-orig", {}, None)
        await handle_action("sid-orig", {"action": "init"})
        original_user_id = fake_sio.first("init_data")["user_id"]
        fake_sio.clear()

        # Attacker on a new SID claims that user_id with junk token.
        await connect(
            "sid-evil",
            {},
            {"user_id": original_user_id, "session_token": "v2.0.deadbeef"},
        )
        await handle_action("sid-evil", {"action": "init"})

        attacker_init = fake_sio.first("init_data")
        assert (
            attacker_init["user_id"] != original_user_id
        ), "server trusted a forged user_id with a bad token"

    async def test_reconnect_with_expired_token_creates_fresh_identity(self, fake_sio, mock_ml):
        """Tokens carry an HMAC over (user_id, issued_at) and the server
        rejects anything older than 7 days. Forge a token from 8 days
        ago with the right HMAC; server must still refuse it."""
        import time as _time

        from app.main import connect, handle_action
        from app.security import (
            DEFAULT_MAX_AGE_SECONDS,
            TOKEN_VERSION,
            _compute_signature,
        )

        # Get a real user_id from a valid init.
        await connect("sid-orig", {}, None)
        await handle_action("sid-orig", {"action": "init"})
        original_user_id = fake_sio.first("init_data")["user_id"]
        fake_sio.clear()

        # Craft an "expired" token using the test signing secret.
        old_ts = int(_time.time()) - (DEFAULT_MAX_AGE_SECONDS + 86400)
        sig = _compute_signature(uuid.UUID(original_user_id), old_ts, "test-signing-secret")
        expired_token = f"{TOKEN_VERSION}.{old_ts}.{sig}"

        await connect(
            "sid-stale",
            {},
            {"user_id": original_user_id, "session_token": expired_token},
        )
        await handle_action("sid-stale", {"action": "init"})

        stale_init = fake_sio.first("init_data")
        assert stale_init["user_id"] != original_user_id, "server accepted an expired token"

    async def test_reconnect_restores_sessions_list(self, fake_sio, mock_ml):
        """End-to-end: create a session, disconnect, reconnect with the
        stored credentials, expect that session_id to appear in
        `init_data.sessions`. This is the most user-visible part of the
        contract — the sidebar must not "lose" the user's history on
        a page refresh."""
        from app.main import connect, disconnect, handle_action

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate("Saved")})
        await connect("sid-keep", {}, None)
        await handle_action("sid-keep", {"action": "init"})
        first_init = fake_sio.first("init_data")
        user_id = first_init["user_id"]
        session_token = first_init["session_token"]

        await handle_action("sid-keep", {"action": "message", "text": "process please"})
        created_session_id = fake_sio.first("result")["session_id"]

        await disconnect("sid-keep")
        fake_sio.clear()

        # Reconnect.
        await connect(
            "sid-back-keep",
            {},
            {"user_id": user_id, "session_token": session_token},
        )
        await handle_action("sid-back-keep", {"action": "init"})

        init_data = fake_sio.first("init_data")
        assert init_data["user_id"] == user_id
        session_ids = [s["session_id"] for s in init_data.get("sessions", [])]
        assert created_session_id in session_ids, (
            f"session {created_session_id} disappeared on reconnect; " f"got: {session_ids!r}"
        )

    async def test_reconnect_allows_editing_session_from_new_sid(self, fake_sio, mock_ml):
        """The harder half of the contract: after reconnect, the user
        can actually CONTINUE the conversation — i.e. /edit on their
        own session works on the new SID."""
        from app.main import connect, disconnect, handle_action

        mock_ml.set({"/classify": _ok_classify(), "/generate": _ok_generate()})
        await connect("sid-old", {}, None)
        await handle_action("sid-old", {"action": "init"})
        first_init = fake_sio.first("init_data")
        user_id = first_init["user_id"]
        token = first_init["session_token"]
        await handle_action("sid-old", {"action": "message", "text": "v1 process"})
        session_id = fake_sio.first("result")["session_id"]

        await disconnect("sid-old")
        fake_sio.clear()

        # Reconnect from a different SID.
        await connect("sid-new", {}, {"user_id": user_id, "session_token": token})
        await handle_action("sid-new", {"action": "init"})
        fake_sio.clear()
        mock_ml.clear_requests()

        # Edit the existing session from the new SID.
        edited_xml = SAMPLE_BPMN_XML.replace("Do Thing", "Edited Thing")
        mock_ml.set({"/classify": _ok_classify(), "/edit": _ok_edit(edited_xml)})
        await handle_action(
            "sid-new",
            {"action": "message", "text": "edit", "session_id": session_id},
        )

        result = fake_sio.first("result")
        assert "Edited Thing" in result["bpmn_xml"]
        assert any(r.url.path == "/edit" for r in mock_ml.requests)


# ---------------------------------------------------------------------------
# pytest config — opt into pytest-asyncio's auto mode for this module.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio
