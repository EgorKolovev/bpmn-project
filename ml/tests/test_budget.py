from app.budget import BudgetTracker, DailyBudgetExceededError


class TestBudgetTracker:
    def test_reserve_finalize_and_release(self, tmp_path):
        tracker = BudgetTracker(
            db_path=str(tmp_path / "usage.sqlite3"),
            daily_limit_usd=5.0,
            input_price_per_million_usd=0.25,
            output_price_per_million_usd=1.50,
            max_output_tokens=8192,
            timezone_name="UTC",
        )

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
        tracker = BudgetTracker(
            db_path=str(tmp_path / "usage.sqlite3"),
            daily_limit_usd=0.01,
            input_price_per_million_usd=0.25,
            output_price_per_million_usd=1.50,
            max_output_tokens=8192,
            timezone_name="UTC",
        )

        try:
            tracker.reserve_for_call(prompt_tokens=1)
        except DailyBudgetExceededError as exc:
            assert "$0.01" in str(exc)
        else:
            raise AssertionError("Expected reserve_for_call to reject the request")
