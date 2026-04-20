import os


# Model: `gemini-3-flash-preview` is the default after benchmarking on
# real 10–13 KB customer PDFs (командировка, отправка документов).
# Quality tier: flash-lite-preview < 2.5-flash ≈ 3-flash-preview, but
# 3-flash-preview is ~2× faster than 2.5-flash at equivalent quality.
# Switch back to `gemini-3.1-flash-lite-preview` for short/simple specs
# if cost matters more than branching richness.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
# 65536 max output tokens. Raised 16384 → 32768 → 65536 — observed that
# on large role-rich specs (13 KB PDFs), gemini-3-flash-preview can
# produce 30 K+ output tokens of combined thinking + BPMN XML.
# Truncation is silent from the model's perspective and shows up as
# finishReason=MAX_TOKENS in our logs. 64 K leaves generous headroom
# while still bounded for cost.
MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "65536"))
# Thinking budget for Gemini 2.5/3.x models:
#   0      → disabled (no thinking, fastest/cheapest)
#   N > 0  → explicit cap in tokens (recommended)
#   -1     → dynamic — DANGEROUS: model may think far longer than our
#            HTTP timeouts, causing timeouts and cascading retries.
# Default 4096 — sweet spot for 3-flash-preview on 10–13 KB specs:
# captures all gateways/cycles while keeping latency ~25–35s. Lower
# values (1024–2048) cause branching regressions on long role-rich
# specs; higher values risk MAX_TOKENS truncation.
# Non-thinking-capable models silently ignore this parameter.
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "4096"))
DAILY_SPEND_LIMIT_USD = float(os.environ.get("DAILY_SPEND_LIMIT_USD", "5.0"))
USAGE_DB_PATH = os.environ.get("USAGE_DB_PATH", "/tmp/bpmn_usage.sqlite3")
USAGE_BUDGET_TIMEZONE = os.environ.get("USAGE_BUDGET_TIMEZONE", "UTC")
# Description / prompt char cap. Raised 12000 → 20000 after client
# feedback: real 10–13 KB PDF specs were getting rejected as too long.
# 20000 matches the backend's MAX_MESSAGE_CHARS. Keep both in lockstep.
REQUEST_CHAR_LIMIT = int(os.environ.get("REQUEST_CHAR_LIMIT", "20000"))
BPMN_XML_CHAR_LIMIT = int(os.environ.get("BPMN_XML_CHAR_LIMIT", "250000"))

# LLM backend: "gemini" (direct Google API) or "polza" (OpenAI-compatible via polza.ai)
LLM_BACKEND = os.environ.get("LLM_BACKEND", "gemini")

# Polza.ai settings
POLZA_API_KEY = os.environ.get("POLZA_API_KEY", "")
POLZA_API_URL = os.environ.get("POLZA_API_URL", "https://polza.ai/api/v1")
POLZA_MODEL = os.environ.get("POLZA_MODEL", "google/gemini-2.5-flash")

# Public list prices per 1M tokens (USD). These are used for the local
# daily-spend guard — the actual invoiced price may differ (esp. via Polza).
# Override with GEMINI_INPUT/OUTPUT_PRICE_PER_1M_USD env vars if needed.
DEFAULT_PRICING_PER_MILLION_USD = {
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
