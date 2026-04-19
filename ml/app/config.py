import os


DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "8192"))
DAILY_SPEND_LIMIT_USD = float(os.environ.get("DAILY_SPEND_LIMIT_USD", "5.0"))
USAGE_DB_PATH = os.environ.get("USAGE_DB_PATH", "/tmp/bpmn_usage.sqlite3")
USAGE_BUDGET_TIMEZONE = os.environ.get("USAGE_BUDGET_TIMEZONE", "UTC")
REQUEST_CHAR_LIMIT = int(os.environ.get("REQUEST_CHAR_LIMIT", "12000"))
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
}

FALLBACK_PRICING = DEFAULT_PRICING_PER_MILLION_USD["gemini-2.5-flash"]


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
