"""Per-token LLM pricing — owns the model→price lookup and env overrides.

Kept separate from `app.config` (which is a pure env-variable surface)
so callers reading a price don't have to depend on the whole config
module and so pricing data has a single, obvious home.

`config.py` re-exports `get_input_price_per_million_usd` and
`get_output_price_per_million_usd` for backwards-compatibility with
any external import — but new code should import from here.
"""

import os

# Public list prices per 1M tokens (USD). These are used for the local
# daily-spend guard — the actual invoiced price may differ (esp. via Polza).
# Override with GEMINI_INPUT/OUTPUT_PRICE_PER_1M_USD env vars if needed.
DEFAULT_PRICING_PER_MILLION_USD: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
    },
    "gemini-2.5-flash-lite": {
        "input": 0.10,
        "output": 0.40,
    },
    "gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.00,
    },
    "gemini-3.1-flash-lite-preview": {
        "input": 0.25,
        "output": 1.50,
    },
    # Preview pricing — invoice numbers will be authoritative. Using
    # a conservative estimate slightly above 2.5-flash until official
    # public pricing is posted (override via GEMINI_*_PRICE_PER_1M_USD).
    "gemini-3-flash-preview": {
        "input": 0.30,
        "output": 2.50,
    },
    "gemini-3-pro-preview": {
        "input": 1.25,
        "output": 10.00,
    },
}

FALLBACK_PRICING = DEFAULT_PRICING_PER_MILLION_USD["gemini-3-flash-preview"]


def get_input_price_per_million_usd(model: str) -> float:
    override = os.environ.get("GEMINI_INPUT_PRICE_PER_1M_USD")
    if override:
        return float(override)
    return DEFAULT_PRICING_PER_MILLION_USD.get(model, FALLBACK_PRICING)["input"]


def get_output_price_per_million_usd(model: str) -> float:
    override = os.environ.get("GEMINI_OUTPUT_PRICE_PER_1M_USD")
    if override:
        return float(override)
    return DEFAULT_PRICING_PER_MILLION_USD.get(model, FALLBACK_PRICING)["output"]
