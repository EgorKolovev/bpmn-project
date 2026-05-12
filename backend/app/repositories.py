"""Repository layer wrapping every DB access for the backend.

Repositories are tiny stateful wrappers around an `AsyncSession`,
instantiated per request:

    async with async_session() as db:
        sessions = await SessionRepository(db).list_for_user(user_id)

The factory `async_session` itself stays where it is in
`app.database` / `app.main`, so existing test monkey-patches keep
working. The point of repos is to take DB queries OUT of the
Socket.IO message handlers — handlers now read like business logic.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Message, Session


class SessionRepository:
    """Reads + writes for the `sessions` table."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        limit: int,
    ) -> list[Session]:
        """Sessions belonging to `user_id`, newest-updated first, capped."""
        result = await self.db.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.updated_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_with_messages(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Session | None:
        """Fetch a single session WITH its `messages` eagerly loaded.

        Returns None if the session doesn't exist OR belongs to another
        user — callers must treat both the same so we don't leak the
        existence of foreign sessions.
        """
        result = await self.db.execute(
            select(Session)
            .options(selectinload(Session.messages))
            .where(Session.id == session_id, Session.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_owner(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Session | None:
        """Same as `get_with_messages` but without preloading `messages`.
        Used by the edit path where only `current_bpmn_xml` is needed.
        """
        result = await self.db.execute(
            select(Session).where(Session.id == session_id, Session.user_id == user_id)
        )
        return result.scalar_one_or_none()

    def add_new(
        self,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
        bpmn_xml: str,
    ) -> Session:
        """Build a `Session` row and stage it via `self.db.add`. Caller
        is responsible for `await self.db.flush()` / `commit()`.
        """
        session = Session(
            id=session_id,
            user_id=user_id,
            name=name,
            current_bpmn_xml=bpmn_xml,
        )
        self.db.add(session)
        return session

    def update_current_bpmn(self, session: Session, bpmn_xml: str) -> None:
        """Update `current_bpmn_xml` on an attached ORM instance and
        re-stage it so the changes flush on commit.
        """
        session.current_bpmn_xml = bpmn_xml
        self.db.add(session)


class MessageRepository:
    """Writes for the `messages` table — reads always go through
    `SessionRepository.get_with_messages` (selectinload-eager).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    def add_user_message(
        self,
        *,
        session_id: uuid.UUID,
        text: str,
        order: int,
    ) -> None:
        self.db.add(
            Message(
                session_id=session_id,
                role="user",
                text=text,
                order=order,
            )
        )

    def add_assistant_message(
        self,
        *,
        session_id: uuid.UUID,
        bpmn_xml: str,
        order: int,
    ) -> None:
        self.db.add(
            Message(
                session_id=session_id,
                role="assistant",
                bpmn_xml=bpmn_xml,
                order=order,
            )
        )
