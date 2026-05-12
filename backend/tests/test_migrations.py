"""Alembic round-trip test — verifies `upgrade head` + `downgrade base`
reach a clean schema state on the real production dialect (Postgres).

Opt-in: requires a reachable Postgres reachable via the `TEST_PG_URL`
env var (set by CI). Skipped silently in regular local runs against
SQLite — those don't need it (SQLite is just for unit-test speed).

CI provisions this via a `services: postgres:16-alpine` block in
`.github/workflows/ci.yml`.
"""

import os

import pytest
from sqlalchemy import inspect

# Skip the whole module if PG isn't configured.
PG_URL = os.environ.get("TEST_PG_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="set TEST_PG_URL to run alembic round-trip tests (e.g. CI-only)",
)


@pytest.fixture
def alembic_config():
    """Build an alembic Config pointing at the test Postgres URL."""
    from pathlib import Path

    from alembic.config import Config

    backend_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    # alembic's own env.py reads `app.config.settings.database_url`, so
    # we override the env var BEFORE the test imports anything.
    cfg.set_main_option("sqlalchemy.url", PG_URL)
    return cfg


def test_upgrade_head_then_downgrade_base(alembic_config, monkeypatch):
    """`alembic upgrade head` builds the full schema; `downgrade base`
    tears it back down. Both must succeed and leave the DB in the
    expected states (full / empty)."""
    # Override settings.database_url so env.py picks up PG too.
    monkeypatch.setenv("DATABASE_URL", PG_URL)

    from alembic import command
    from sqlalchemy import create_engine

    # Upgrade to head.
    command.upgrade(alembic_config, "head")

    sync_url = PG_URL.replace("+asyncpg", "")
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "sessions" in tables, f"sessions table missing: {tables}"
        assert "messages" in tables, f"messages table missing: {tables}"
        assert "alembic_version" in tables
    finally:
        engine.dispose()

    # Downgrade to base.
    command.downgrade(alembic_config, "base")

    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        # alembic_version stays (the table holds the empty-history marker).
        assert "sessions" not in tables
        assert "messages" not in tables
    finally:
        engine.dispose()
