# Build stage: install dependencies via uv
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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

# SHELL REMOVAL - Security hardening (v4.2)
# Replace shells with stubs; keep python3.real for entrypoint/healthcheck
RUN for shell in sh bash; do \
        if [ -f "/bin/$shell" ]; then \
            cp "/bin/$shell" "/bin/${shell}.real" && \
            echo '#!/bin/sh' > "/bin/$shell" && \
            echo 'echo "Shell disabled for security"' >> "/bin/$shell" && \
            echo 'exit 1' >> "/bin/$shell" && \
            chmod +x "/bin/$shell"; \
        fi; \
    done

# Create non-root user
RUN groupadd -r mcproxy && useradd -r -g mcproxy mcproxy

# Create directory structure
RUN mkdir -p /app/config /app/data /app/cache && chown -R mcproxy:mcproxy /app

# Copy application code (packages + root modules)
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

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD /usr/bin/python3.real -c "import urllib.request; urllib.request.urlopen('http://localhost:12010/health', timeout=5)" || exit 1

ENTRYPOINT ["/usr/bin/python3.real", "main.py"]
CMD ["--log", "--port", "12010", "--config", "/app/config/mcproxy.json"]
