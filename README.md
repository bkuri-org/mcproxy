# MCProxy

> A lightweight MCP gateway that aggregates multiple stdio and HTTP MCP servers through namespaced endpoints.

**Status**: v5.0.3 | **Python**: 3.11+ | **Port**: 12010

---

## Features

| Feature | Description |
|---------|-------------|
| **Code Mode API** | Single `mcproxy` meta-tool with execute/search/inspect/help actions |
| **Dual Transport** | Stdio and HTTP MCP servers — connect to pre-existing services or spawn child processes |
| **Namespace Isolation** | Group servers by privilege level with access control and `!` force-include |
| **API Key Auth** | Agent auth with encrypted credential storage and rotation |
| **Blocklist Security** | Server validation with blocked/risky classification |
| **Manifest System** | Capability registry with caching, TypeScript type generation, and event hooks |
| **Sandbox Pool** | Pre-warmed sandbox instances with configurable pool sizing |
| **Session Stash** | Per-session key-value store with TTL for cross-call state |
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

> See [Deployment Options](#deployment-options) for bare-metal, Docker Compose, and Quadlet setups.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        MCProxy Gateway                          │
│                                                                 │
│  ┌───────────────┐ ┌───────────────┐ ┌───────────────────────┐ │
│  │  Auth          │ │  Blocklist    │ │  Manifest             │ │
│  │  (API keys +  │ │  (blocked/    │ │  (registry + cache +  │ │
│  │   credentials)│ │   risky)      │ │   TypeScript gen)     │ │
│  └───────┬───────┘ └───────┬───────┘ └───────────┬───────────┘ │
│          └─────────────────┼─────────────────────┘             │
│                            │                                    │
│  ┌─────────────────────────┼────────────────────────────────┐  │
│  │  Sandbox Pool           │    Session Stash               │  │
│  │  (pre-warmed executors  │    (per-session KV + TTL)      │  │
│  │   with code validation) │                                │  │
│  └─────────────────────────┼────────────────────────────────┘  │
│                            │                                    │
│  ┌─────────────────────────┼────────────────────────────────┐  │
│  │                  Server Manager                           │  │
│  │                                                           │  │
│  │  ┌─ Stdio ─────────────┐  ┌─ HTTP ─────────────────────┐ │  │
│  │  │ wikipedia  youtube  │  │ jesse (per-tool timeouts)   │ │  │
│  │  │ perplexity coinstats│  │ any Streamable HTTP server  │ │  │
│  │  │ llms_txt  more...   │  │                             │ │  │
│  │  └────────────────────┘  └─────────────────────────────┘ │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Configuration

### Servers

MCProxy supports two server types:

```json
{
  "servers": [
    {
      "name": "wikipedia",
      "command": "/usr/bin/npx",
      "args": ["-y", "wikipedia-mcp"],
      "timeout": 60
    },
    {
      "name": "jesse",
      "type": "http",
      "url": "http://localhost:12011/mcp",
      "timeout": 350,
      "tool_timeout": 600,
      "tool_timeouts": {
        "backtest": 900,
        "optimize": 1200
      }
    }
  ]
}
```

### Namespaces & Groups

```json
{
  "namespaces": {
    "docs": {"servers": ["wikipedia", "llms_txt"], "isolated": false},
    "trading": {"servers": ["jesse"], "isolated": true},
    "home": {"servers": ["home_assistant"], "isolated": false}
  },
  "groups": {
    "research": {"namespaces": ["thinking", "docs", "web", "financial"]},
    "maxitrader": {"namespaces": ["thinking", "financial", "docs", "web", "!trading"]}
  }
}
```

The `!` prefix on a namespace in a group means **force-include** — isolated namespaces are normally excluded from groups unless explicitly prefixed.

### Sandbox

```json
{
  "sandbox": {
    "timeout_secs": 900,
    "pool": {
      "size": 3,
      "max_size": 10,
      "idle_timeout_secs": 300
    }
  }
}
```

---

## Security

Defense-in-depth with blocklist validation and sandbox hardening:

- **Blocklist validation** at startup (blocked/risky/unclassified)
- **Sandbox code validation** — blocked imports (`os`, `subprocess`, `socket`, …), blocked builtins (`eval`, `exec`, `open`, …), blocked dunder attributes
- **JS-style auto-conversion** — agents commonly send `{key: "value"}` instead of `{"key": "value"}`; the sandbox auto-fixes this and other common syntax errors
- **Shell removal** in container (sh/bash/python disabled)
- **Capability dropping** (CapDrop=ALL) and **filesystem isolation** (ProtectHome, ReadOnlyRootfs)

### Blocked Servers

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

## Deployment Options

MCProxy supports three deployment modes depending on your stage:

| Mode | Best For | Config Format | Server Config |
|------|----------|--------------|---------------|
| **Bare Metal** | Development & active syncing | `command`/`args` (stdio) | Root `mcproxy.json` |
| **Docker Compose** | Full containerized stack | `url` (HTTP adapters) | `config/mcproxy.json` |
| **Quadlet** | Single-gateway production | `url` (HTTP adapters) | `/srv/containers/mcproxy/config/mcproxy.json` |

---

### 🖥️  Bare Metal (Development)

Runs the gateway directly alongside adapter processes. Best for rapid iteration and syncing between local and server.

```bash
# Terminal 1: gateway
python main.py --log --port 12010 --config mcproxy.json

# Terminal 2+: adapters (one per MCP server)
python adapter.py --port 12020 --host 127.0.0.1 -- npx -y wikipedia-mcp
python adapter.py --port 12021 --host 127.0.0.1 -- npx -y @modelcontextprotocol/server-sequential-thinking
# ...
```

The root `mcproxy.json` uses `command`/`args` (stdio format) so mcproxy spawns servers directly. When using the adapter-based architecture (bare metal with `adapter.py`), use a config with `url` fields pointing to the adapter ports — or let mcproxy spawn stdio servers directly.

---

### 🐳  Docker Compose (Full Stack)

Runs the hardened gateway + all adapter backends as containers on a shared bridge network.

```bash
# Build both images
sudo docker compose build mcproxy       # Hardened gateway (no shell/python)
sudo docker compose build adapters       # Permissive adapters (has node, uv, python)

# Or build everything at once
sudo docker compose build

# Launch
sudo docker compose up -d

# Check health
curl http://localhost:12010/health
```

**How it works:**

```
                   ┌─────────────────────────────┐
                   │      mcproxy (hardened)      │
                   │  port 12010, no shell/python │
                   └──────────┬──────────────────┘
                              │ container names over mcproxy-net
          ┌───────────────────┼───────────────────────┐
          ▼                   ▼                       ▼
   ┌──────────────┐   ┌──────────────┐      ┌──────────────┐
   │ adapter-*    │   │ adapter-*    │  …   │ adapter-*    │
   │ (permissive) │   │ (permissive) │      │ (permissive) │
   │ npx, node,   │   │ uvx, python  │      │ …            │
   │ uv           │   │              │      │              │
   └──────────────┘   └──────────────┘      └──────────────┘
```

- The **gateway** (`docker-compose.yml` → `mcproxy` service) uses `Dockerfile` — hardened with shell/python disabled, `CapDrop=ALL`, read-only rootfs
- **Adapters** (every `adapter-*` service) use `Dockerfile.adapter` — intentionally permissive with node, npm, uv, and python for spawning subprocesses
- Communication is over `mcproxy-net` bridge by container name
- The `config/mcproxy.json` uses `url` format: `"url": "http://adapter-wikipedia:12027/mcp"`

**External services** (jesse at `http://host.docker.internal:12011/mcp`, not_human_search, zilliqa_insights) connect via their existing URLs.

---

### 📦  Quadlet (Gateway Only — Production)

Systemd-managed single container for the gateway. Adapters managed separately (or connect to existing services).

```bash
# 1. Build the image
sudo podman build -t localhost/mcproxy:latest .

# 2. Create data directories
sudo mkdir -p /srv/containers/mcproxy/{config,data,cache}

# 3. Deploy config (url-based, pointing to adapter backends)
sudo cp config/mcproxy.json /srv/containers/mcproxy/config/

# 4. Deploy Quadlet
sudo cp mcproxy.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start mcproxy
```

**Before deploying, edit `mcproxy.container`:**

| Placeholder | Replace With |
|-------------|-------------|
| `192.168.50.X` | Your server's host IP (e.g., `192.168.50.70` for server1) |
| `10.90.0.20` | An available IP on the `lan-core` bridge subnet |

**Key differences from Compose:**

| Aspect | Compose | Quadlet |
|--------|---------|--------|
| Scope | Full stack (gateway + adapters) | Gateway only |
| Network | `mcproxy-net` (auto-created) | `lan-core` (existing bridge) |
| Port exposure | `0.0.0.0:12010` | `192.168.50.X:12010` (host-pinned) |
| Restart | `unless-stopped` | `always` via systemd |
| Adaptee mgmt | Compose handles lifecycle | Handled externally (systemd, scripts, etc.) |

---

### 🔁  Switching Modes

The key difference between modes is the **config format**:

```jsonc
// Bare metal (stdio) — mcproxy spawns subprocesses directly
{ "command": "/usr/bin/npx", "args": ["-y", "wikipedia-mcp"] }

// Containerized (HTTP) — mcproxy connects to adapter containers
{ "url": "http://adapter-wikipedia:12027/mcp" }
```

The root `mcproxy.json` is the bare-metal reference. The `config/mcproxy.json` is the containerized reference. Both produce the same namespace/group structure — only the server transport differs.

---

## Configuration

### Timeouts

Each server has a configurable `timeout` (in seconds) that controls how long mcproxy waits for a response from the MCP subprocess. The default is **120 seconds**.

```json
{
  "name": "my_server",
  "command": "/usr/bin/npx",
  "args": ["-y", "some-mcp-server"],
  "timeout": 120
}
```

For long-running operations (e.g., backtesting), you can set higher timeouts or use `tool_timeouts` for per-tool overrides:

```json
{
  "name": "jesse",
  "timeout": 350,
  "tool_timeout": 600,
  "tool_timeouts": {
    "backtest": 900,
    "optimize": 1200
  }
}
```

> **Note:** If you're calling mcproxy through an MCP client (e.g., opencode, Claude Desktop), ensure the client's own timeout is set higher than mcproxy's server timeout, or the client may terminate the connection before mcproxy responds. For example, in opencode's `opencode.json`, set `"timeout": 120000` (120s in milliseconds).

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
