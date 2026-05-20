# Multi-stage build for MCProxy
# Stage 1: Build dependencies
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Production image
FROM python:3.11-slim

# Create non-root user for security
RUN groupadd -r mcproxy && useradd -r -g mcproxy mcproxy

# Set working directory
WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /root/.local /home/mcproxy/.local
ENV PATH=/home/mcproxy/.local/bin:$PATH

# Install curl for healthcheck (only tool available after shell/python removal)
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# SHELL REMOVAL - Security hardening for v4.2
# Disable shell access to prevent arbitrary code execution
# Only uv and node remain available for MCP servers
RUN if [ -f /bin/sh ]; then \
    echo '#!/bin/sh\necho "Shell disabled for security"\nexit 1' > /bin/sh.disabled && \
    chmod +x /bin/sh.disabled && \
    (mv /bin/sh /bin/sh.real 2>/dev/null || true) && \
    ln -sf /bin/sh.disabled /bin/sh; fi && \
    if [ -f /bin/bash ]; then \
    echo '#!/bin/bash\necho "Shell disabled for security"\nexit 1' > /bin/bash.disabled && \
    chmod +x /bin/bash.disabled && \
    (mv /bin/bash /bin/bash.real 2>/dev/null || true) && \
    ln -sf /bin/bash.disabled /bin/bash; fi && \
    if [ -f /usr/bin/python3 ]; then \
    echo '#!/usr/bin/python3\nprint("Python disabled for security")\nexit 1' > /usr/bin/python.disabled && \
    chmod +x /usr/bin/python.disabled && \
    (mv /usr/bin/python3 /usr/bin/python3.real 2>/dev/null || true) && \
    ln -sf /usr/bin/python.disabled /usr/bin/python3; fi

# Create directory structure
RUN mkdir -p /app/config /app/data /app/cache && chown -R mcproxy:mcproxy /app

# Copy application code
COPY --chown=mcproxy:mcproxy *.py .

# Switch to non-root user
USER mcproxy

# Expose the SSE endpoint port
EXPOSE 12010

# Health check (uses curl since python/shell are disabled for security)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:12010/health || exit 1

# Run MCProxy
ENTRYPOINT ["python", "main.py"]
CMD ["--log", "--config", "/app/config/mcproxy.json"]