#!/bin/bash
# MCProxy Container Deploy — run on server2 after git push
# Usage: ssh server2-auto /srv/containers/mcproxy/deploy.sh
set -euo pipefail

echo "=== MCProxy Container Deploy ==="
cd /srv/containers/mcproxy

# 1. Sync code from main branch
git fetch origin
git reset --hard origin/main

# 2. Rebuild container image
sudo podman build -t localhost/mcproxy:latest . 2>&1 | tail -2

# 3. Copy Quadlet
sudo cp mcproxy.container /etc/containers/systemd/mcproxy.container

# 4. Reload systemd and restart
sudo systemctl daemon-reload
sudo systemctl restart mcproxy.service

# 5. Wait for startup with bounded retry loop
MAX_RETRIES=12
SLEEP_SEC=5
echo "  → Verifying health..."
for i in $(seq 1 $MAX_RETRIES); do
    if curl -sf http://localhost:12010/health > /dev/null 2>&1; then
        echo "  ✓ Health check passed (attempt $i)"
        echo ""
        echo "✅ Deployment complete!"
        curl -s http://localhost:12010/health | python3 -m json.tool 2>/dev/null || echo "Health endpoint OK"
        exit 0
    fi
    if [ $i -lt $MAX_RETRIES ]; then
        echo "  ⏳ Waiting for mcproxy (attempt $i/$MAX_RETRIES)..."
        sleep $SLEEP_SEC
    fi
done

echo "❌ mcproxy health check FAILED after $MAX_RETRIES attempts"
sudo podman logs mcproxy 2>&1 | tail -20
exit 1