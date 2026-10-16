# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#   builder  - installs locked, hash-verified wheels into a virtualenv
#   runtime  - slim image, non-root user, no compilers, no package manager cache
#
# For fully reproducible builds pin the base image by digest in CI, e.g.
#   --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest>
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements/prod.txt /tmp/requirements.txt
# --require-hashes: every artifact must match the lockfile (supply-chain check).
# --only-binary: no sdists are built, so no compiler is needed or trusted.
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="inventory-api" \
      org.opencontainers.image.description="Multi-warehouse inventory REST API"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    APP_ENV=production \
    LOG_FORMAT=json

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# Code is owned by root and read-only for the runtime user.
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]

# Exec form: uvicorn is PID 1 and receives SIGTERM directly, stops accepting
# connections, drains in-flight requests (up to 20 s) and closes the DB pool.
# Worker count: WEB_CONCURRENCY. Trusted proxies: FORWARDED_ALLOW_IPS.
STOPSIGNAL SIGTERM
CMD ["uvicorn", "app.main:app_factory", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header", "--no-access-log", \
     "--log-config", "app/uvicorn_logging.json", \
     "--timeout-graceful-shutdown", "20"]
