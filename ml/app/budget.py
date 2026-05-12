"""Daily LLM spend tracking — SQLAlchemy implementation.

Supports both SQLite (test fixtures, default ml/data/ file) and
Postgres (potential future deployment) via the same ORM layer:

  * SQLite: an `event.listens_for(engine, "begin")` hook swaps SQLite's
    default `BEGIN DEFERRED` for `BEGIN IMMEDIATE`, which serialises
    in-process writers and matches the prior raw-SQL semantics.
  * Postgres: row-level locking via `SELECT ... FOR UPDATE` inside
    `Session.begin()`. Concurrent reservations queue at the row level.

Both paths guarantee the cap can't be raced past.

Constructor accepts EITHER `db_url` (preferred) OR `db_path` (compat
shim for tests that still pass `db_path=str(tmp_path / "usage.sqlite3")`).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, DailyBudget

NANODOLLARS_PER_USD = 1_000_000_000
TOKENS_PER_MILLION = 1_000_000
PROMPT_TOKEN_RESERVE_MARGIN = 256


def _usd_to_nanodollars(amount_usd: float) -> int:
    return int(Decimal(str(amount_usd)) * Decimal(str(NANODOLLARS_PER_USD)))


def _per_million_usd_to_nanodollars_per_token(amount_usd: float) -> int:
    return int(
        Decimal(str(amount_usd))
        * Decimal(str(NANODOLLARS_PER_USD))
        / Decimal(str(TOKENS_PER_MILLION))
    )


def format_usd_from_nanodollars(amount_nanodollars: int) -> str:
    amount = Decimal(amount_nanodollars) / Decimal(str(NANODOLLARS_PER_USD))
    return f"{amount:.2f}"


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    day_key: str
    reserved_cost_nanodollars: int


class DailyBudgetExceededError(Exception):
    def __init__(self, limit_usd: float, day_key: str, timezone_name: str):
        self.limit_usd = limit_usd
        self.day_key = day_key
        self.timezone_name = timezone_name
        super().__init__(
            f"Daily usage cap of ${limit_usd:.2f} reached for {day_key} ({timezone_name})."
        )


def _build_db_url(db_path: str | None, db_url: str | None) -> str:
    """Resolve constructor args → SQLAlchemy URL.

    `db_url` wins. If only `db_path` is given (legacy callers, tests),
    build `sqlite:///<absolute path>`. The parent directory is created
    eagerly so a fresh deploy doesn't crash on first reservation.
    """
    if db_url:
        return db_url
    if db_path:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    raise ValueError("BudgetTracker requires either db_url or db_path")


class BudgetTracker:
    """Synchronous SQLAlchemy-backed daily budget tracker.

    All public methods are synchronous; async callers (LLMClient)
    invoke them via `run_in_executor` to avoid blocking the event loop.
    """

    def __init__(
        self,
        daily_limit_usd: float,
        input_price_per_million_usd: float,
        output_price_per_million_usd: float,
        max_output_tokens: int,
        timezone_name: str = "UTC",
        *,
        db_url: str | None = None,
        db_path: str | None = None,
    ):
        self.daily_limit_nanodollars = _usd_to_nanodollars(daily_limit_usd)
        self.daily_limit_usd = daily_limit_usd
        self.input_price_per_token_nanodollars = _per_million_usd_to_nanodollars_per_token(
            input_price_per_million_usd
        )
        self.output_price_per_token_nanodollars = _per_million_usd_to_nanodollars_per_token(
            output_price_per_million_usd
        )
        self.max_output_tokens = max_output_tokens
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)

        resolved_url = _build_db_url(db_path=db_path, db_url=db_url)
        # SQLite + multi-thread: `check_same_thread=False` is OK because
        # `BEGIN IMMEDIATE` (set by the event hook below) plus
        # SQLAlchemy's per-thread connection pool ensures only one
        # writer is active at a time.
        connect_args = {"check_same_thread": False} if resolved_url.startswith("sqlite") else {}
        self.engine = create_engine(
            resolved_url,
            future=True,
            connect_args=connect_args,
        )

        # Swap SQLite's default BEGIN DEFERRED → BEGIN IMMEDIATE so
        # concurrent writers serialise instead of racing.
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "begin")
            def _begin_immediate(conn) -> None:  # pragma: no cover — hook
                conn.exec_driver_sql("BEGIN IMMEDIATE")

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, future=True)

    # -- timezone helpers ----------------------------------------------------

    def _today_key(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def _now(self) -> datetime:
        return datetime.now(self.timezone)

    # -- cost math (unchanged from prior implementation) --------------------

    def _estimate_call_cost_nanodollars(self, prompt_tokens: int) -> int:
        reserved_prompt_tokens = max(prompt_tokens, 0) + PROMPT_TOKEN_RESERVE_MARGIN
        return (
            reserved_prompt_tokens * self.input_price_per_token_nanodollars
            + self.max_output_tokens * self.output_price_per_token_nanodollars
        )

    def _actual_call_cost_nanodollars(
        self,
        prompt_tokens: int,
        output_tokens: int,
    ) -> int:
        return (
            max(prompt_tokens, 0) * self.input_price_per_token_nanodollars
            + max(output_tokens, 0) * self.output_price_per_token_nanodollars
        )

    # -- row helpers --------------------------------------------------------

    def _fetch_for_update(self, session: Session, day_key: str) -> DailyBudget | None:
        """SELECT the row for `day_key`, with `FOR UPDATE` on Postgres."""
        stmt = select(DailyBudget).where(DailyBudget.day == day_key)
        if self.engine.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        return session.execute(stmt).scalar_one_or_none()

    # -- public API ---------------------------------------------------------

    def reserve_for_call(self, prompt_tokens: int) -> BudgetReservation:
        reservation = BudgetReservation(
            reservation_id=str(uuid.uuid4()),
            day_key=self._today_key(),
            reserved_cost_nanodollars=self._estimate_call_cost_nanodollars(prompt_tokens),
        )

        with self.session_factory.begin() as session:
            row = self._fetch_for_update(session, reservation.day_key)
            actual_cost = row.actual_cost_nanodollars if row else 0
            reserved_cost = row.reserved_cost_nanodollars if row else 0

            if (
                actual_cost + reserved_cost + reservation.reserved_cost_nanodollars
                > self.daily_limit_nanodollars
            ):
                raise DailyBudgetExceededError(
                    limit_usd=self.daily_limit_usd,
                    day_key=reservation.day_key,
                    timezone_name=self.timezone_name,
                )

            if row is None:
                session.add(
                    DailyBudget(
                        day=reservation.day_key,
                        actual_cost_nanodollars=0,
                        reserved_cost_nanodollars=reservation.reserved_cost_nanodollars,
                        prompt_tokens=0,
                        output_tokens=0,
                        request_count=0,
                        updated_at=self._now(),
                    )
                )
            else:
                row.reserved_cost_nanodollars += reservation.reserved_cost_nanodollars
                row.updated_at = self._now()

        return reservation

    def finalize_call(
        self,
        reservation: BudgetReservation,
        prompt_tokens: int,
        output_tokens: int,
    ) -> int:
        actual_cost_nanodollars = self._actual_call_cost_nanodollars(
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )

        with self.session_factory.begin() as session:
            row = self._fetch_for_update(session, reservation.day_key)
            if row is not None:
                row.reserved_cost_nanodollars = max(
                    row.reserved_cost_nanodollars - reservation.reserved_cost_nanodollars,
                    0,
                )
                row.actual_cost_nanodollars += actual_cost_nanodollars
                row.prompt_tokens += max(prompt_tokens, 0)
                row.output_tokens += max(output_tokens, 0)
                row.request_count += 1
                row.updated_at = self._now()

        return actual_cost_nanodollars

    def release_reservation(self, reservation: BudgetReservation) -> None:
        with self.session_factory.begin() as session:
            row = self._fetch_for_update(session, reservation.day_key)
            if row is not None:
                row.reserved_cost_nanodollars = max(
                    row.reserved_cost_nanodollars - reservation.reserved_cost_nanodollars,
                    0,
                )
                row.updated_at = self._now()
