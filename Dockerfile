# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.13

# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install into a self-contained venv so the runtime stage copies one directory
# and never depends on the interpreter's site-packages path.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# INSTALL_DEV=true adds pytest/ruff/mypy and the fixture libraries. Only the
# `tests` compose service sets it — the runtime image ships neither the test
# runner nor the linters, which it previously did because there was one
# undifferentiated requirements.txt.
ARG INSTALL_DEV=false

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Match the host user so bind mounts (api-dev) stay writable.
ARG UID=1000
ARG GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy app source
COPY . .

# Create non-root user. The mkdir must run before the chown so that named
# volumes mounted at these paths inherit civic:civic ownership on first init.
RUN groupadd -g ${GID} civic \
    && useradd -u ${UID} -g civic -m -s /usr/sbin/nologin civic \
    && mkdir -p /app/uploads /app/credentials /app/logs \
    && chown -R civic:civic /app /opt/venv

USER civic

EXPOSE 8000

# Liveness, not readiness: GET / pings the database and returns 503 when it is
# down, which would flap the container on any transient DB blip.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

# Default command for a bare `docker run`. Compose overrides it with the real
# flags. Single worker here because background jobs are gated on
# RUN_BACKGROUND_JOBS, which defaults true — see the `api`/`jobs` split in
# docker-compose.yml, where the API runs multiple workers with jobs disabled.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
