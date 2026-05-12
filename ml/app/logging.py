"""Structured logging via structlog — mirror of `backend/app/logging.py`.

Two output modes (selected by `LOG_FORMAT` env var):
  * `json` — production, one JSON object per line, parseable by Promtail.
  * `console` (default) — colorized, human-readable for local dev.

Call `configure_logging()` once at process start.
"""

import logging
import os
import sys

import structlog


def configure_logging() -> None:
    """Wire structlog + stdlib logging. Idempotent."""
    log_format = os.environ.get("LOG_FORMAT", "console").lower()
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
        wrapper_class=structlog.stdlib.BoundLogger,
    )

    if log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    # Quiet down noisy third-party loggers in prod.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel("WARNING" if log_format == "json" else log_level)
