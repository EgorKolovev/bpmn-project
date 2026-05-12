"""Structured logging via structlog.

Two output modes:
  * `LOG_FORMAT=json` — JSON renderer, one log entry per line. Suited
    for Promtail → Loki → Grafana (the docker compose stack scrapes
    stdout JSON lines and parses out the structured fields).
  * `LOG_FORMAT=console` (default in development) — colorized human
    output. No JSON parsing, easier to skim.

Call `configure_logging()` exactly once at process start. After that,
any module can `import structlog; logger = structlog.get_logger(__name__)`
and use `.info("...", extra_field=value)` — the `extra_field` becomes a
JSON key in prod or a `key=value` pair in dev.
"""

import logging
import os
import sys

import structlog


def configure_logging() -> None:
    """Wire structlog + stdlib logging. Idempotent."""
    log_format = os.environ.get("LOG_FORMAT", "console").lower()
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    # Route stdlib log records (e.g. from uvicorn, sqlalchemy, asyncpg)
    # through structlog's `ProcessorFormatter` so they appear in the
    # same stream and (if json mode) carry the same structured shape.
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
    # Replace any previously-installed handlers so re-configuration
    # doesn't double-log.
    root.handlers = [handler]
    root.setLevel(log_level)

    # Quiet down noisy third-party loggers in prod.
    for noisy in ("httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel("WARNING" if log_format == "json" else log_level)
