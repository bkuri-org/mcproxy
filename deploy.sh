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

# 5. Wait for startup
sleep 5

# 6. Verify
if curl -sf http://localhost:12010/health > /dev/null 2>&1; then
    echo "✅ mcproxy is healthy"
    curl -s http://localhost:12010/health | python3 -m json.tool 2>/dev/null || echo "Health endpoint OK"
else
    echo "❌ mcproxy health check FAILED"
    sudo podman logs mcproxy 2>&1 | tail -20
    exit 1
fi