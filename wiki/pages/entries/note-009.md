---
id: note-009
tags: [readme]
created: 2026-05-10T09:32:58.947225+00:00
source: README.md
---

# README

# MCProxy v4.2

> A lightweight MCP gateway that aggregates multiple stdio MCP servers through namespaced SSE endpoints.

**Status**: v4.2 - Security Hardening | **Python**: 3.11+ | **Port**: 12010

---

## Features

| Feature | Description |
|---------|-------------|
| **Code Mode API** | Single `mcproxy` meta-tool with execute/search/inspect actions |
| **Namespace Isolation** | Group servers by privilege level with access control |
| **Static API Key Auth** | Agent auth with encrypted credential storage (v4.2) |
| **Blocklist Security** | Server validation with blocked/risky classification (v4.2) |
| **Hot-Reload** | Add/remove servers without dropping connections |
| **Dual Mode** | HTTP/SSE endpoint OR native MCP server over stdio |

---

## Quick Start

```bash
# Setup
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Run
python main.py --log --config mcproxy.json
```

### Docker

```bash
docker build -t localhost/mcproxy:latest .
docker run -d -p 12010:12010 \
  -v $(pwd)/config:/app/config:Z \
  localhost/mcproxy:latest
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      MCProxy Gateway                        │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │   Auth v4.1  │    │ Blocklist    │    │  Code Mode   │  │
│  │  (JWT + cred) │    │  v4.2        │    │  (execute)   │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│         │                   │                   │          │
│         └───────────────────┼───────────────────┘          │
│                             │                              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              Server Manager                           │  │
│  │  wikipedia  perplexity  coinstats  youtube  ...       │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## Security (v4.2)

MCProxy v4.2 includes defense-in-depth security:

- **Blocklist validation** at startup
- **Shell removal** in container (sh/bash/python disabled)
- **Capability dropping** (CapDrop=ALL)
- **Filesystem isolation** (ProtectHome, ReadOnlyRootfs)

### Blocked Servers

Critical-risk servers are blocked by default:

```json
{
  "@executeautomation/tmux-mcp-server": "blocked (arbitrary shell execution)"
}
```

### Risky Servers (require acknowledgment)

```json
{
  "security": {
    "allow_risky_servers": true,
    "risky_server_acknowledgments": {
      "playwright": "Required for browser automation"
    }
  }
}
```

---

## Documentation

| Document | Purpose |
|----------|---------|
| [AGENTS.md](AGENTS.md) | Agent guidelines (quick reference) |
| [docs/EXAMPLES.md](docs/EXAMPLES.md) | Detailed usage examples |
| [docs/HISTORY.md](docs/HISTORY.md) | Archived documentation |
| [ROADMAP.md](ROADMAP.md) | Future plans and milestones |

## Also See

- [CHANGELOG.md](CHANGELOG.md) - Version history

---

## CLI Options

```bash
python main.py [OPTIONS]
  --stdio              Native MCP server over stdio
  --log                Log to stdout (default: syslog)
  --port PORT          Port (default: 12010)
  --config PATH        Config file path
  --no-reload          Disable hot-reload
```

---

## Deployment

```bash
# Quadlet (Systemd)
sudo cp mcproxy.container /etc/containers/systemd/
sudo systemctl enable --now mcproxy.service

# Auto-deploy (after initial setup)
git push  # Hook handles rest
```

---

## Troubleshooting

```bash
# Health check
curl http://localhost:12010/health

# Validate config
python -m json.tool mcproxy.json

# Check logs
journalctl -u mcproxy.service -f
```

---

## Dependencies

```
fastapi>=0.104.0
uvicorn>=0.24.0
python-json-logger>=2.0.7
fastmcp>=0.1.0
orjson>=3.9.0
cryptography>=42.0.0
python-jose[cryptography]>=3.3.0
bcrypt>=4.0.0
aiohttp>=3.9.0
```

---

## Acknowledgments

MCProxy v2.0's Code Mode architecture was inspired by **[Forgemax](https://github.com/postrv/forgemax)** — a Rust-based MCP gateway that introduced the concept of collapsing N servers × M tools into just 2 meta-tools (`search` + `execute`) for massive context reduction.

---

**GitHub**: https://github.com/bkuri/mcproxy  
**Issues**: https://github.com/bkuri/mcproxy/issues

