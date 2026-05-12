"""SQLAlchemy ORM models for the ml service.

Currently a single table: `daily_usage`, the per-day budget ledger that
`BudgetTracker` maintains. The next commit (`C14`) rewrites
`BudgetTracker` to use this ORM model instead of raw SQL — this commit
is purely additive so it can land separately and reviewers can read the
new schema definition in isolation.
"""

from sqlalchemy import BigInteger, Column, DateTime, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class DailyBudget(Base):
    """One row per UTC day (or per `USAGE_BUDGET_TIMEZONE`'s day).

    Columns are nanodollar-denominated where possible (1 USD = 10^9 nano)
    to keep all arithmetic on integers — float prices were causing
    cap-checks to round-trip incorrectly under concurrent reservations.

      * `day` — ISO date string, primary key. String (not Date) to match
        the raw-SQL incumbent so the cutover commit doesn't have to
        translate stored values.
      * `actual_cost_nanodollars` — finalised cost after `finalize_call`.
      * `reserved_cost_nanodollars` — held-but-not-yet-finalised cost
        (a rolling "in flight" sum; `release_reservation` decrements).
      * `prompt_tokens`, `output_tokens` — telemetry, not used for cap.
      * `request_count` — count of `finalize_call` invocations.
      * `updated_at` — last mutation timestamp (TZ-aware).
    """

    __tablename__ = "daily_usage"

    day = Column(String, primary_key=True)
    actual_cost_nanodollars = Column(BigInteger, nullable=False, default=0)
    reserved_cost_nanodollars = Column(BigInteger, nullable=False, default=0)
    prompt_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    request_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False)
