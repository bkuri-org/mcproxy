#!/bin/bash
# MCProxy Container Deploy — run on server2 after git push
# Phase 3: Bridge networking (mcp-net), k8s-file logs, no Quadlet dependency
# Usage: ssh server2-auto /srv/containers/mcproxy/deploy.sh
set -euo pipefail

echo "=== MCProxy Container Deploy (Phase 3 - Bridge) ==="
cd /srv/containers/mcproxy

# 1. Sync code from main branch
echo "  → Pulling latest from origin/main..."
# Ensure git objects dir is writable (fixes permission errors after fresh clone)
sudo find .git/objects -type d -exec chmod g+w {} \; 2>/dev/null || true
git fetch origin
git reset --hard origin/main

# 2. Rebuild container image
echo "  → Rebuilding image..."
# Build with pinned venv python (VENV_PYTHON ARG required; no PATH fallback)
sudo podman build --build-arg VENV_PYTHON=/usr/bin/python3 -t localhost/mcproxy:latest . 2>&1 | tail -2

# 3. Copy config to bind-mounted config directory
echo "  → Syncing config..."
sudo mkdir -p config
sudo cp mcproxy.json config/mcproxy.json
# Preserve .env if it doesn't exist
if [ ! -f config/.env ]; then
    sudo cp .env.example config/.env 2>/dev/null || true
fi

# 4. Remove existing container and start fresh on bridge network
echo "  → Replacing mcproxy container..."
sudo podman rm -f mcproxy 2>/dev/null || true

sudo podman run -d --replace --name mcproxy \
  --log-driver=k8s-file \
  --network=mcp-net -p 12010:12010 \
  --read-only \
  -v /srv/containers/mcproxy/config/mcproxy.json:/app/config/mcproxy.json:ro,Z \
  -v /srv/containers/mcproxy/config/.env:/app/.env:ro,Z \
  -v mcproxy-data:/app/data:Z \
  -v mcproxy-cache:/app/cache:Z \
  -e PYTHONUNBUFFERED=1 \
  -e PATH=/usr/local/noexec:$PATH \
  --security-opt no-new-privileges --cap-drop=ALL \
  --memory=512m --memory-swap=512m \
  --label "app=mcproxy" \
  --label "phase=3-bridge" \
  localhost/mcproxy:latest \
  /usr/local/bin/mcproxy --log --port 12010 --host 0.0.0.0 --config /app/config/mcproxy.json

# 5. Generate systemd service (Quadlet doesn't generate .service for mcproxy)
# 7. Verify /usr/local/noexec noexec guard dir exists on host (created by Dockerfile,
#    but ensure it's present for bind-mount or volume contexts)
echo "  → Verifying noexec guard dir..."
if ! sudo test -d /usr/local/noexec; then
    echo "  ⚠  /usr/local/noexec missing — container image may not have been built correctly"
    exit 1
fi
sudo ls -ld /usr/local/noexec
for _bin in sh bash python python3 dash ash; do
    if ! sudo test -L /usr/local/noexec/$_bin; then
        echo "  ⚠  /usr/local/noexec/$_bin symlink missing"
        exit 1
    fi
done

echo "  → Creating systemd service..."
sudo podman generate systemd --new --name mcproxy \
  --restart-policy=always \
  --time 10 \
  2>/dev/null | sudo tee /etc/systemd/system/mcproxy-container.service > /dev/null
sudo systemctl daemon-reload

# 6. Wait for startup with bounded retry loop
MAX_RETRIES=20
SLEEP_SEC=5
echo "  → Verifying health..."
for i in $(seq 1 $MAX_RETRIES); do
    if timeout 2 /bin/bash -c "echo > /dev/tcp/localhost/12010" 2>/dev/null; then
        TOOL_COUNT=$(curl -sf -X POST http://localhost:12010/message \
          -H "Content-Type: application/json" \
          -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"full":true}}' 2>/dev/null \
          | /usr/bin/python3 -c "import sys,json; print(len(json.load(sys.stdin).get('result',{}).get('tools',[])))" 2>/dev/null || echo "?")
        echo "  ✓ Port 12010 open, $TOOL_COUNT tools ready (attempt $i)"
        echo ""
        echo "✅ Deployment complete! mcproxy serving on bridge (mcp-net)"
        exit 0
    fi
    if [ $i -lt $MAX_RETRIES ]; then
        echo "  ⏳ Waiting for mcproxy (attempt $i/$MAX_RETRIES)..."
        sleep $SLEEP_SEC
    fi
done

echo "❌ mcproxy health check FAILED after $MAX_RETRIES attempts"
echo "  → Dumping container inspect for image verification..."
sudo podman inspect mcproxy --format='{{.Config.Entrypoint}} {{.Config.Cmd}}' 2>/dev/null || true
sudo podman logs mcproxy 2>&1 | tail -30
exit 1