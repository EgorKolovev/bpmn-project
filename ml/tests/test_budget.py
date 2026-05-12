"""BudgetTracker tests — run against SQLite by default, or against the
URL in `BUDGET_TEST_DB_URL` (used by the CI `test-ml-budget-pg` job
to exercise the Postgres `SELECT ... FOR UPDATE` branch).
"""

import asyncio
import os
import threading
import uuid

import pytest

from app.budget import BudgetTracker, DailyBudgetExceededError


def _tracker(tmp_path, daily_limit_usd: float = 5.0) -> BudgetTracker:
    """Build a BudgetTracker against either SQLite (default) or the
    URL given in `BUDGET_TEST_DB_URL`.

    The Postgres path uses a per-test random `search_path` so concurrent
    test runs don't clobber each other's `daily_usage` rows.
    """
    pg_url = os.environ.get("BUDGET_TEST_DB_URL")
    if pg_url:
        # Each test gets a fresh schema in the same PG instance.
        schema = f"budget_test_{uuid.uuid4().hex[:12]}"
        url_with_schema = (
            f"{pg_url}?options=-c%20search_path%3D{schema}"
            if "?" not in pg_url
            else f"{pg_url}&options=-c%20search_path%3D{schema}"
        )
        # Create the schema first via a bare psycopg2 connection.
        from sqlalchemy import create_engine, text

        bootstrap = create_engine(pg_url, future=True)
        with bootstrap.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        bootstrap.dispose()
        return BudgetTracker(
            db_url=url_with_schema,
            daily_limit_usd=daily_limit_usd,
            input_price_per_million_usd=0.25,
            output_price_per_million_usd=1.50,
            max_output_tokens=8192,
            timezone_name="UTC",
        )

    # Default: SQLite file under tmp_path.
    return BudgetTracker(
        db_path=str(tmp_path / "usage.sqlite3"),
        daily_limit_usd=daily_limit_usd,
        input_price_per_million_usd=0.25,
        output_price_per_million_usd=1.50,
        max_output_tokens=8192,
        timezone_name="UTC",
    )


class TestBudgetTracker:
    def test_reserve_finalize_and_release(self, tmp_path):
        tracker = _tracker(tmp_path)

        reservation = tracker.reserve_for_call(prompt_tokens=1000)
        actual_cost = tracker.finalize_call(
            reservation=reservation,
            prompt_tokens=1000,
            output_tokens=2000,
        )

        assert actual_cost > 0

        second = tracker.reserve_for_call(prompt_tokens=100)
        tracker.release_reservation(second)

    def test_rejects_when_daily_limit_would_be_exceeded(self, tmp_path):
        tracker = _tracker(tmp_path, daily_limit_usd=0.01)

        try:
            tracker.reserve_for_call(prompt_tokens=1)
        except DailyBudgetExceededError as exc:
            assert "$0.01" in str(exc)
        else:
            raise AssertionError("Expected reserve_for_call to reject the request")


class TestBudgetTrackerConcurrency:
    """`BudgetTracker` is the single throttle the ml service uses to
    cap LLM spend per day. Concurrent writers are the classic place to
    hit subtle locking / lost-update bugs, so these tests hammer it
    from many tasks at once. The same tests run on:

      * SQLite (default) — `BEGIN IMMEDIATE` via SQLAlchemy event hook.
      * Postgres (CI `test-ml-budget-pg`) — `SELECT ... FOR UPDATE`.
    """

    def test_concurrent_reservations_no_deadlock(self, tmp_path):
        """20 threads each reserving + finalizing should all complete
        without deadlock or DB-lock timeouts."""
        tracker = _tracker(tmp_path, daily_limit_usd=100.0)
        N = 20

        errors: list[Exception] = []

        def worker():
            try:
                reservation = tracker.reserve_for_call(prompt_tokens=100)
                tracker.finalize_call(reservation=reservation, prompt_tokens=100, output_tokens=200)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            assert not t.is_alive(), "deadlock — worker thread did not finish"

        assert errors == [], f"unexpected errors: {errors!r}"

    def test_concurrent_reservations_respect_daily_cap(self, tmp_path):
        """With a daily cap of $0.10 and call cost ~$0.012 (8K output
        tokens × $1.50/1M = $0.0123), ~8 reservations should succeed
        and the rest must raise DailyBudgetExceededError.

        Validates that no two reservations can squeeze past the cap.
        """
        tracker = _tracker(tmp_path, daily_limit_usd=0.10)
        N = 30
        approved: list[bool] = []
        lock = threading.Lock()

        def worker():
            try:
                tracker.reserve_for_call(prompt_tokens=100)
                with lock:
                    approved.append(True)
            except DailyBudgetExceededError:
                with lock:
                    approved.append(False)

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        approved_count = sum(approved)
        rejected_count = len(approved) - approved_count
        assert approved_count + rejected_count == N
        assert approved_count > 0, "no reservation succeeded"
        assert (
            rejected_count > 0
        ), f"cap not enforced — all {N} reservations approved despite $0.10 cap"

    def test_release_after_reserve_returns_budget_to_cap(self, tmp_path):
        """A released reservation should free its share of the cap for
        subsequent calls. Drain the cap, release one, confirm fresh
        reservation succeeds."""
        tracker = _tracker(tmp_path, daily_limit_usd=0.10)

        held_reservations = []
        while True:
            try:
                held_reservations.append(tracker.reserve_for_call(prompt_tokens=100))
            except DailyBudgetExceededError:
                break
            if len(held_reservations) > 100:
                pytest.fail("cap never enforced after 100 reservations")

        assert len(held_reservations) >= 1, "cap rejected the very first reservation"

        with pytest.raises(DailyBudgetExceededError):
            tracker.reserve_for_call(prompt_tokens=100)

        tracker.release_reservation(held_reservations[0])
        new_reservation = tracker.reserve_for_call(prompt_tokens=100)
        assert new_reservation is not None

        tracker.release_reservation(new_reservation)
        for r in held_reservations[1:]:
            tracker.release_reservation(r)

    @pytest.mark.asyncio
    async def test_async_concurrent_reservations(self, tmp_path):
        """Same hammer test but via asyncio + run_in_executor — matches
        how `LLMClient._call_llm` uses the tracker from a coroutine."""
        tracker = _tracker(tmp_path, daily_limit_usd=100.0)
        N = 20

        async def one():
            loop = asyncio.get_event_loop()
            reservation = await loop.run_in_executor(None, tracker.reserve_for_call, 100)
            await loop.run_in_executor(None, tracker.finalize_call, reservation, 100, 200)
            return True

        results = await asyncio.gather(*(one() for _ in range(N)))
        assert all(results)
