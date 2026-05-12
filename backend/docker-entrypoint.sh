#!/bin/sh
# Backend container entrypoint.
#
# Schema management strategy:
#   * Fresh DB (no tables): `alembic upgrade head` creates everything.
#   * Existing DB created by the old `Base.metadata.create_all` lifespan
#     (no `alembic_version` table yet): stamp 0001_initial first so the
#     CREATE TABLE in that revision doesn't fail, then upgrade.
#   * Existing DB already managed by alembic: upgrade is a no-op.
#
# Both stamp and upgrade are idempotent re-runs.
set -e

echo "[entrypoint] Bootstrapping alembic state if needed…"
# Use asyncpg (already in runtime deps) for the probe — avoids pulling
# in psycopg2 just to inspect the schema once at boot.
python <<'PY'
import asyncio
import os
import subprocess
import urllib.parse

import asyncpg


async def main() -> None:
    raw_url = os.environ["DATABASE_URL"]
    # Strip SQLAlchemy driver suffix (`+asyncpg`) and parse out parts.
    parsed = urllib.parse.urlparse(raw_url.replace("+asyncpg", ""))
    if parsed.scheme not in ("postgres", "postgresql"):
        # SQLite / other — alembic upgrade handles everything itself.
        return

    conn = await asyncpg.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database=parsed.path.lstrip("/"),
    )
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        tables = {row["tablename"] for row in rows}
    finally:
        await conn.close()

    if "sessions" in tables and "alembic_version" not in tables:
        # Pre-alembic deploy: stamp the initial revision so the upgrade
        # below treats the schema as already at that revision.
        print("[entrypoint] Stamping 0001_initial (pre-alembic schema detected).")
        subprocess.check_call(["alembic", "stamp", "0001_initial"])


asyncio.run(main())
PY

echo "[entrypoint] Running alembic upgrade head…"
alembic upgrade head

echo "[entrypoint] Starting uvicorn…"
exec uvicorn app.main:combined_app --host 0.0.0.0 --port 8000
