# Build stage: install dependencies via uv
FROM python:3.11-slim AS builder

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
RUN uv pip install --system --no-cache .

# Production stage
FROM python:3.11-slim

WORKDIR /app

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



USER mcproxy

EXPOSE 12010

# Health check (uses curl since shell is disabled for security)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:12010/health || exit 1

ENTRYPOINT ["python3", "main.py"]
CMD ["--log", "--port", "12010", "--config", "/app/config/mcproxy.json"]