import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


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


class BudgetTracker:
    def __init__(
        self,
        db_path: str,
        daily_limit_usd: float,
        input_price_per_million_usd: float,
        output_price_per_million_usd: float,
        max_output_tokens: int,
        timezone_name: str = "UTC",
    ):
        self.db_path = Path(db_path)
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
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path, timeout=30, isolation_level=None)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    day TEXT PRIMARY KEY,
                    actual_cost_nanodollars INTEGER NOT NULL DEFAULT 0,
                    reserved_cost_nanodollars INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _today_key(self) -> str:
        return datetime.now(self.timezone).date().isoformat()

    def _now_iso(self) -> str:
        return datetime.now(self.timezone).isoformat()

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

    def reserve_for_call(self, prompt_tokens: int) -> BudgetReservation:
        reservation = BudgetReservation(
            reservation_id=str(uuid.uuid4()),
            day_key=self._today_key(),
            reserved_cost_nanodollars=self._estimate_call_cost_nanodollars(prompt_tokens),
        )

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT actual_cost_nanodollars, reserved_cost_nanodollars
                FROM daily_usage
                WHERE day = ?
                """,
                (reservation.day_key,),
            ).fetchone()
            actual_cost = row[0] if row else 0
            reserved_cost = row[1] if row else 0

            if actual_cost + reserved_cost + reservation.reserved_cost_nanodollars > self.daily_limit_nanodollars:
                conn.execute("ROLLBACK")
                raise DailyBudgetExceededError(
                    limit_usd=self.daily_limit_usd,
                    day_key=reservation.day_key,
                    timezone_name=self.timezone_name,
                )

            if row is None:
                conn.execute(
                    """
                    INSERT INTO daily_usage (
                        day,
                        actual_cost_nanodollars,
                        reserved_cost_nanodollars,
                        updated_at
                    ) VALUES (?, 0, ?, ?)
                    """,
                    (
                        reservation.day_key,
                        reservation.reserved_cost_nanodollars,
                        self._now_iso(),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE daily_usage
                    SET reserved_cost_nanodollars = reserved_cost_nanodollars + ?,
                        updated_at = ?
                    WHERE day = ?
                    """,
                    (
                        reservation.reserved_cost_nanodollars,
                        self._now_iso(),
                        reservation.day_key,
                    ),
                )
            conn.execute("COMMIT")

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
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE daily_usage
                SET reserved_cost_nanodollars = MAX(reserved_cost_nanodollars - ?, 0),
                    actual_cost_nanodollars = actual_cost_nanodollars + ?,
                    prompt_tokens = prompt_tokens + ?,
                    output_tokens = output_tokens + ?,
                    request_count = request_count + 1,
                    updated_at = ?
                WHERE day = ?
                """,
                (
                    reservation.reserved_cost_nanodollars,
                    actual_cost_nanodollars,
                    max(prompt_tokens, 0),
                    max(output_tokens, 0),
                    self._now_iso(),
                    reservation.day_key,
                ),
            )
            conn.execute("COMMIT")
        return actual_cost_nanodollars

    def release_reservation(self, reservation: BudgetReservation) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE daily_usage
                SET reserved_cost_nanodollars = MAX(reserved_cost_nanodollars - ?, 0),
                    updated_at = ?
                WHERE day = ?
                """,
                (
                    reservation.reserved_cost_nanodollars,
                    self._now_iso(),
                    reservation.day_key,
                ),
            )
            conn.execute("COMMIT")
