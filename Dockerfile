# Build stage: install dependencies via uv
FROM python:3.11-slim AS builder

# Pin the Python interpreter path for all build/tooling steps; fail-fast if missing
ARG VENV_PYTHON=/usr/local/bin/python3.11
RUN [ ! -x "$VENV_PYTHON" ] && { echo "FATAL: $VENV_PYTHON not executable" >&2; exit 1; } || true

WORKDIR /build

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv

# Copy project files for build
COPY pyproject.toml README.md .
# Copy requirements.txt if it exists (some deps are listed here too)
COPY requirements.txt ./
# Install into system site-packages
RUN uv pip install --system --python "$VENV_PYTHON" --no-cache .

# Production stage
FROM python:3.11-slim

# Pin the Python interpreter path; fail-fast if missing; no PATH fallback
ARG VENV_PYTHON=/usr/local/bin/python3.11
ENV VENV_PYTHON=$VENV_PYTHON
RUN [ ! -x "$VENV_PYTHON" ] && { echo "FATAL: $VENV_PYTHON not executable" >&2; exit 1; } || true

WORKDIR /app

# Create noexec trap directory: root-owned 0555 with /dev/null symlinks for common
# shells/interpreters. PATH-prepended ONLY in spawned MCP server environments
# (in application code), never in global PATH.
RUN mkdir -p /usr/local/noexec && \
    chmod 0555 /usr/local/noexec && \
    for cmd in sh bash python python3 dash ash; do \
        ln -sf /dev/null /usr/local/noexec/"$cmd"; \
    done

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Create non-root user and directory structure
RUN groupadd -r mcproxy && useradd -r -g mcproxy mcproxy && \
    mkdir -p /app/config /app/data /app/cache && \
    chown -R mcproxy:mcproxy /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY --chown=mcproxy:mcproxy *.py .
COPY --chown=mcproxy:mcproxy auth/ auth/
COPY --chown=mcproxy:mcproxy manifest/ manifest/
COPY --chown=mcproxy:mcproxy sandbox/ sandbox/
COPY --chown=mcproxy:mcproxy server/ server/
COPY --chown=mcproxy:mcproxy server/handlers/ server/handlers/
COPY --chown=mcproxy:mcproxy server/handlers/tools/ server/handlers/tools/
COPY --chown=mcproxy:mcproxy utils/ utils/
COPY --chown=mcproxy:mcproxy reasoning/ reasoning/



USER mcproxy

# Verify non-root execution (fails build if USER directive did not take effect)
RUN [ "$(id -u)" != "0" ] || { echo "FATAL: image must not run as root" >&2; exit 1; }

EXPOSE 12010

# Health check (uses curl since shell is disabled for security)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:12010/health || exit 1

ENTRYPOINT ["/usr/local/bin/python3.11", "main.py"]
CMD ["--log", "--port", "12010", "--config", "/app/config/mcproxy.json"]