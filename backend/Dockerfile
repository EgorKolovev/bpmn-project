# Multi-stage build for the backend service.
#   * builder — wheel-compiles every runtime dep (gives us a fully-
#     populated /wheels cache); also pre-pulls any C-extension wheels.
#   * runtime — copies only the wheel cache + app source; no pytest,
#     no test corpus, no build-essential.
#
# CI runs pytest on the host (see .github/workflows/ci.yml), so removing
# the test stack from the prod image has zero impact on test coverage.

# ---------- Stage 1: builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Compiler + headers needed for any sdist-only deps to wheel-compile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install runtime deps from the prebuilt wheels — no compiler, no network.
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# App source only — no tests/, no pytest.ini.
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /data && chown appuser:appuser /data
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
