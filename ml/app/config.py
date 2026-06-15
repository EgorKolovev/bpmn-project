"""Runtime configuration — read from environment variables at import.

Pricing data + the per-model price helpers live in `app.pricing`; they
are re-exported here so callers that import `from app.config import
get_input_price_per_million_usd` (or `DEFAULT_PRICING_PER_MILLION_USD`)
keep working unchanged. New code should import from `app.pricing`.
"""

import os

from app.pricing import (
    DEFAULT_PRICING_PER_MILLION_USD,
    FALLBACK_PRICING,
    get_input_price_per_million_usd,
    get_output_price_per_million_usd,
)

# Re-export for backward compatibility.
__all__ = [
    "BPMN_XML_CHAR_LIMIT",
    "CLASSIFY_THINKING_BUDGET",
    "DAILY_SPEND_LIMIT_USD",
    "DEFAULT_MODEL",
    "DEFAULT_PRICING_PER_MILLION_USD",
    "EDIT_THINKING_BUDGET",
    "FALLBACK_PRICING",
    "GEMINI_THINKING_BUDGET",
    "LLM_BACKEND",
    "LLM_HTTP_TIMEOUT",
    "MAX_OUTPUT_TOKENS",
    "POLZA_API_KEY",
    "POLZA_API_URL",
    "POLZA_MODEL",
    "REQUEST_CHAR_LIMIT",
    "USAGE_BUDGET_TIMEZONE",
    "USAGE_DB_PATH",
    "get_input_price_per_million_usd",
    "get_output_price_per_million_usd",
]

# Model: `gemini-3-flash-preview` is the default after benchmarking on
# real 10–13 KB customer PDFs (командировка, отправка документов).
# Quality tier: flash-lite-preview < 2.5-flash ≈ 3-flash-preview, but
# 3-flash-preview is ~2× faster than 2.5-flash at equivalent quality.
# Switch back to `gemini-3.1-flash-lite-preview` for short/simple specs
# if cost matters more than branching richness.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
# 32768 max output tokens (reasoning + BPMN XML, combined). This is a
# HARD LATENCY BOUND: gemini-3-flash-preview emits ~300 tokens/sec, so
# 32K ≈ 110s worst case — safely under LLM_HTTP_TIMEOUT.
#
# Was 65536, which let a request run ~218s and blow past the HTTP
# timeout → ReadTimeout → 502 with the spent tokens wasted (see
# prod incident 2026-06-14: edit/generate timing out). On Gemini the
# combined thinking+XML rarely exceeds ~20K with thinkingBudget=4096,
# so 32K keeps generous headroom while capping the tail.
MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "32768"))
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
# Per-operation thinking budgets. Generate (from scratch) benefits from
# reasoning to get lanes + branching right. Edit applies a delta to an
# existing diagram and classify is a boolean — both need little/no
# reasoning, and on gemini-3-flash thinking DOMINATES latency:
# measured edit at budget=4096 → ~124s, at budget=0 → ~6s (same output).
# So edit/classify default to 0 (no thinking) for snappy interactive UX.
# Tunable if complex edits ever need reasoning.
EDIT_THINKING_BUDGET = int(os.environ.get("EDIT_THINKING_BUDGET", "0"))
CLASSIFY_THINKING_BUDGET = int(os.environ.get("CLASSIFY_THINKING_BUDGET", "0"))
# HTTP timeout (seconds) for ONE LLM API call. Was hardcoded 180s in
# each backend — shorter than gemini-3-flash-preview can legitimately
# take on a large diagram, especially via Polza where reasoning.effort
# is NOT token-capped. When the call exceeds this, httpx raises
# ReadTimeout → LLMClientError → 502, and the already-spent tokens are
# wasted. 240s gives headroom above the ~110s worst case implied by
# MAX_OUTPUT_TOKENS. Keep it >= worst-case generation time.
LLM_HTTP_TIMEOUT = float(os.environ.get("LLM_HTTP_TIMEOUT", "240"))
DAILY_SPEND_LIMIT_USD = float(os.environ.get("DAILY_SPEND_LIMIT_USD", "5.0"))
USAGE_DB_PATH = os.environ.get("USAGE_DB_PATH", "/tmp/bpmn_usage.sqlite3")
USAGE_BUDGET_TIMEZONE = os.environ.get("USAGE_BUDGET_TIMEZONE", "UTC")
# Description / prompt char cap. Raised 12000 → 20000 after client
# feedback: real 10–13 KB PDF specs were getting rejected as too long.
# 20000 matches the backend's MAX_MESSAGE_CHARS. Keep both in lockstep.
REQUEST_CHAR_LIMIT = int(os.environ.get("REQUEST_CHAR_LIMIT", "20000"))
BPMN_XML_CHAR_LIMIT = int(os.environ.get("BPMN_XML_CHAR_LIMIT", "250000"))

# LLM backend: "gemini" (direct Google API) or "polza" (OpenAI-compatible via polza.ai).
# Gemini is the default: `thinkingBudget` is a HARD token cap there, so
# reasoning latency is bounded and predictable. Polza maps the budget to
# `reasoning.effort` (low/medium/high), which is NOT token-capped — the
# same config reasons far longer and can exceed the HTTP timeout. Polza
# remains a fallback (flip LLM_BACKEND=polza if Gemini access drops).
LLM_BACKEND = os.environ.get("LLM_BACKEND", "gemini")

# Polza.ai settings
POLZA_API_KEY = os.environ.get("POLZA_API_KEY", "")
POLZA_API_URL = os.environ.get("POLZA_API_URL", "https://polza.ai/api/v1")
POLZA_MODEL = os.environ.get("POLZA_MODEL", "google/gemini-2.5-flash")
