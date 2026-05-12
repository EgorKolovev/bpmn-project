"""Alembic environment — async-aware, reads DATABASE_URL from app.config.

Runs migrations against the same engine the production app uses, but
synchronously: alembic v1.x does not natively run async migrations, so
we open a sync connection via `engine.begin().run_sync(do_migrations)`.

For SQLite (tests), this falls back to the standard sync engine —
alembic isn't intended to drive the test fixture (`_create_all_for_tests`
in `app.database` does that for speed).
"""

# Make `app.*` importable when alembic is run from backend/.
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import models  # noqa: E402, F401 — ensures Base.metadata is populated
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the placeholder URL from alembic.ini with the real one.
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no DB connection, just SQL)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Async path — used for asyncpg URLs (production Postgres)."""
    connectable: AsyncEngine = create_async_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Sync path — for `sqlite:///...` or any non-async URL."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)


def main() -> None:
    if context.is_offline_mode():
        run_migrations_offline()
        return

    url = config.get_main_option("sqlalchemy.url") or ""
    if "+asyncpg" in url or "+aiosqlite" in url:
        import asyncio

        asyncio.run(run_async_migrations())
    else:
        run_migrations_online()


main()
