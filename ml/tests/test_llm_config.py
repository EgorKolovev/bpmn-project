"""Regression tests for LLM backend config (timeouts + thinking budget).

These protect against accidentally re-introducing:
  * thinkingBudget=-1 (unbounded dynamic → HTTP timeouts)
  * 60-second HTTP timeouts (too short for complex thinking + long XML)
"""
import os

os.environ.setdefault("GEMINI_API_KEY", "test-key-for-unit-tests")

import pytest

from app import config
from app.llm import GeminiBackend, PolzaBackend


class TestThinkingBudgetDefault:
    def test_default_is_explicit_positive(self):
        """Default MUST be an explicit positive token cap — never -1 (dynamic)
        because dynamic thinking can exceed our HTTP timeouts."""
        assert config.GEMINI_THINKING_BUDGET > 0, (
            f"GEMINI_THINKING_BUDGET default must be > 0, got {config.GEMINI_THINKING_BUDGET}. "
            "Setting it to -1 (dynamic) previously caused 33-minute test hangs."
        )

    def test_default_within_reasonable_range(self):
        """Default budget should be in a sensible range — not too low to be
        useless, not so high that latency explodes or output truncates.
        Upper bound pushed to 8192 to accommodate gemini-3-flash-preview
        at 4096 (current default) and leave headroom for operator
        experimentation up to 8192."""
        assert 512 <= config.GEMINI_THINKING_BUDGET <= 8192, (
            f"GEMINI_THINKING_BUDGET = {config.GEMINI_THINKING_BUDGET} is outside "
            "sane range [512, 8192]. See docs for why."
        )


class TestMaxOutputTokensDefault:
    def test_default_large_enough_for_long_bpmn(self):
        """MAX_OUTPUT_TOKENS must cover thinking tokens + BPMN XML.
        Observed: at thinkingBudget=4096 on 3-flash, thoughts alone can
        consume ~4–8K tokens, leaving room for a 3–5K-token XML payload.
        16384 is too tight at this tier — truncates on real 13 KB PDFs.
        Regression guard: must be >= 24576 so we never regress back."""
        assert config.MAX_OUTPUT_TOKENS >= 24576, (
            f"MAX_OUTPUT_TOKENS = {config.MAX_OUTPUT_TOKENS} is too low for "
            "gemini-3-flash-preview with thinkingBudget=4096. See PDF "
            "benchmark in REPORT.md — truncates on real customer specs."
        )

    def test_env_override_works(self, monkeypatch):
        """Operators can override via env without touching code."""
        import importlib
        monkeypatch.setenv("GEMINI_THINKING_BUDGET", "4096")
        importlib.reload(config)
        try:
            assert config.GEMINI_THINKING_BUDGET == 4096
        finally:
            # Restore to default by reloading without the env
            monkeypatch.delenv("GEMINI_THINKING_BUDGET", raising=False)
            importlib.reload(config)


class TestBackendTimeout:
    def test_gemini_backend_timeout_at_least_120s(self):
        """Generous HTTP timeout — must be >= 120s. Complex thinking + long
        BPMN output on flash-lite can take >60s legitimately."""
        backend = GeminiBackend(api_key="dummy", model="gemini-2.5-flash", max_output_tokens=1024)
        try:
            assert backend.http_client.timeout.read >= 120.0, (
                f"Gemini HTTP timeout = {backend.http_client.timeout.read}s is too tight"
            )
        finally:
            # Don't leak the AsyncClient
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(backend.close())
                loop.close()
            except Exception:
                pass

    def test_polza_backend_timeout_at_least_120s(self):
        backend = PolzaBackend(
            api_key="dummy", model="x/y", base_url="http://localhost", max_output_tokens=1024,
        )
        try:
            assert backend.http_client.timeout.read >= 120.0
        finally:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(backend.close())
                loop.close()
            except Exception:
                pass


class TestPayloadIncludesThinkingConfig:
    def test_gemini_payload_has_thinking_budget(self):
        backend = GeminiBackend(api_key="dummy", model="gemini-2.5-flash", max_output_tokens=512)
        payload = backend._build_payload("system", "user")
        tc = payload["generationConfig"].get("thinkingConfig")
        assert tc is not None, "generationConfig must contain thinkingConfig"
        assert "thinkingBudget" in tc
        assert tc["thinkingBudget"] == config.GEMINI_THINKING_BUDGET
