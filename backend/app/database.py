from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
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


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
