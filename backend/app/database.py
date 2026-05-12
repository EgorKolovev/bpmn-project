from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import DATABASE_URL

_pg_kwargs = (
    {"pool_size": 20, "max_overflow": 5, "pool_pre_ping": True}
    if DATABASE_URL.startswith("postgresql")
    else {}
)
engine = create_async_engine(DATABASE_URL, echo=False, **_pg_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def _create_all_for_tests() -> None:
    """Create every table from `Base.metadata` on the bound engine.

    **Tests only.** Production runs `alembic upgrade head` at deploy
    time. The autouse `init_db_per_test` fixture in
    `backend/tests/test_integration_backend.py` calls this for SQLite
    speed: running alembic per-test against in-memory SQLite was
    10× slower without exercising migration squash quality (which is
    what `tests/test_migrations.py` covers separately).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# Back-compat alias: the autouse test fixture still calls `init_db()`.
# Production code no longer calls this — lifespan stopped doing it.
init_db = _create_all_for_tests


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
