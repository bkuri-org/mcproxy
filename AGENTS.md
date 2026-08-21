# MCProxy - Agent Guidelines

> **Status**: v5.2.0
> **MCProxy** is a lightweight MCP gateway that aggregates stdio and HTTP MCP servers through namespaced endpoints.

> **Configs**: the repo's `mcproxy.json` is a minimal example. The live instance runs on server2 (192.168.50.71:12010); its config lives at `/srv/containers/mcproxy/config/mcproxy.json` and is maintained there — never sync it into the repo.

> **Remotes**: `origin` = git.lan (canonical; main protected, PR+squash per gtd). `github` = backup mirror — never the source of deploys. Deploys happen on server2 after merge (`git fetch && git reset --hard origin/main && podman build && systemctl restart mcproxy`).

## Quick Reference

### IMPORTANT: MCP Protocol

MCProxy uses MCP (JSON-RPC 2.0), **not REST**:

| Endpoint | Type | Purpose |
|----------|------|---------|
| `GET /health` | REST | Health check |
| `POST /sse` | MCP | Main endpoint |
| `POST /sse/{namespace}` | MCP | Namespaced endpoint |

### Code Mode

Single tool: `mcproxy(action='execute'|'search'|'inspect'|'help', ...)`

```python
# Execute
mcproxy(action='execute', code='api.server("wikipedia").search(query="python")', namespace='dev')

# Search
mcproxy(action='search', query='wikipedia')

# Inspect
mcproxy(action='inspect', server='wikipedia', tool='search')

# Help
mcproxy(action='help', topic='sandbox')
```

### Authentication

```bash
# Get token
curl -X POST http://192.168.50.71:12010/oauth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=YOUR_ID" \
  -d "client_secret=YOUR_SECRET"

# Use token
curl -X POST http://192.168.50.71:12010/sse \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Namespace: dev" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Code Mode API

### Actions

| Action | Purpose |
|--------|---------|
| `execute` | Run Python code with tool access |
| `search` | Find tools by query |
| `inspect` | Get tool schemas |
| `help` | Get documentation |

### Usage

```python
# Execute code
result = mcproxy(action='execute', code='...', namespace='dev')

# Search tools
results = mcproxy(action='search', query='wikipedia')

# Inspect schema
schema = mcproxy(action='inspect', server='wikipedia', tool='search')
```

### Syntax: KEYWORD arguments only

Tool calls use **keyword arguments only**. Positional dicts will fail.

```python
# ✓ Correct — keyword args
api.server('wikipedia').search(query='Python')

# ✗ Wrong — positional dict
api.server('wikipedia').search({'query': 'Python'})

# ✓ Unpack a dict with **
api.server('wikipedia').search(**my_dict)
```

### Parallel Execution

```python
results = parallel([
    lambda: api.server('s1').tool1(),
    lambda: api.server('s2').tool2(),
])
```

### Response Types

Auto-unwrapped — no need for `result['content'][0]['text']`:
- **String**: Direct file contents, messages
- **List**: Direct iteration
- **Dict**: Direct key access

### Sandbox Auto-Fixes

The sandbox auto-corrects common agent syntax mistakes:
- **JS-style object literals**: `{key: "value"}` → `{"key": "value"}`
- **JS-style booleans**: `true` / `false` → `True` / `False`
- **Tool call errors** produce helpful hints with the correct syntax

### Session Stash

Per-session key-value store persists across `execute()` calls:

```python
# Store data (available in subsequent calls within the same session)
api.stash("my_key", {"data": [1, 2, 3]})

# Retrieve in a later call
data = api.unstash("my_key")
```

## Authentication

Static API key authentication with encrypted credential storage:

```
Agent → API Key → MCProxy → Credential → Tool Execution
         (static)           (injected)
```

### Config

```json
{
  "auth": {
    "enabled": true,
    "credentials_db": "data/credentials.db",
    "agents_db": "data/agents.db",
    "admin_key_env": "MCPROXY_ADMIN_KEY"
  }
}
```

### Agent Management

```python
from auth import AgentRegistry
registry = AgentRegistry("data/agents.db")

# Register — automatically generates API key
creds = registry.register(name="agent", allowed_scopes=["scope"], namespace="dev")
# Returns: {client_id, client_secret, api_key}

# Update scopes
registry.update_scopes("agent_id", ["scope1", "scope2"])

# Rotate API key
new_key = registry.rotate_api_key("agent_id")
```

## Admin API

### Endpoint Overview

```
GET  /admin/agents                  List all agents
GET  /admin/agents/{id}             Get agent details
GET  /admin/agents/{id}/api-key     Check if API key exists
POST /admin/agents/{id}/api-key     Generate/rotate API key
DELETE /admin/agents/{id}/api-key   Revoke API key
POST /admin/agents/{id}/rotate      Rotate secret
POST /admin/agents/{id}/enable      Enable agent
POST /admin/agents/{id}/disable     Disable agent
DELETE /admin/agents/{id}           Delete agent
```

### Authentication

Requests from **localhost** (127.0.0.1) are automatically authorized when `MCPROXY_ADMIN_KEY` is not set.

For remote access, use the `X-Admin-Key` header:

```bash
curl -H "X-Admin-Key: your-secret-key" http://192.168.50.71:12010/admin/agents
```

> **SECURITY WARNING**: Without `MCPROXY_ADMIN_KEY` set, admin endpoints are only accessible from localhost. If you expose MCProxy to the network without setting an admin key, anyone can access admin endpoints.

### Examples

```bash
# List agents (from localhost)
curl http://127.0.0.1:12010/admin/agents

# With admin key
curl -H "X-Admin-Key: your-secret-key" http://192.168.50.71:12010/admin/agents

# Rotate agent secret
curl -X POST -H "X-Admin-Key: your-secret-key" \
  http://192.168.50.71:12010/admin/agents/{agent_id}/rotate

# Rotate with re-auth required
curl -X POST -H "X-Admin-Key: your-secret-key" \
  "http://192.168.50.71:12010/admin/agents/{agent_id}/rotate?reauth=true"
```

### Environment Variables

- `MCPROXY_ADMIN_KEY` — The admin key (required for production)
- `auth.admin_key_env` — Config option to customize env var name
- `auth.rotate_reauth` — Config option to require re-auth after rotation

## Security

Defense-in-depth with blocklist validation and sandbox hardening.

### Blocklist

Servers are validated at startup:

- **Blocked**: Critical risk servers cannot start
- **Risky**: Elevated privilege servers require acknowledgment
- **Unclassified**: Warnings logged

### Configuration

```json
{
  "security": {
    "blocklist_enabled": true,
    "blocklist_url": "https://raw.githubusercontent.com/mcproxy/blocklist/main/blocklist.json",
    "blocklist_sync_interval": 3600,
    "allow_risky_servers": false,
    "risky_server_acknowledgments": {
      "playwright": "Required for browser automation"
    }
  }
}
```

### Sandbox Restrictions

- **Blocked imports**: `os`, `sys`, `subprocess`, `socket`, `http`, `urllib`, `requests`, `pickle`, `importlib`, …
- **Blocked builtins**: `eval()`, `exec()`, `compile()`, `open()`, `__import__()`, `breakpoint()`, `getattr()`, `setattr()`, …
- **Blocked dunder attributes**: `__class__`, `__bases__`, `__globals__`, `__dict__`, `__mro__`, …
- **Shell access disabled** in container — no sh/bash/python
- **Credentials isolated** — API keys injected at execution, never exposed
- **Namespace isolation** — access limited to assigned namespace

## Namespaces & Groups

### Configuration

```json
{
  "namespaces": {
    "docs": {"servers": ["wikipedia", "llms_txt"], "isolated": false},
    "trading": {"servers": ["jesse"], "isolated": true},
    "home": {"servers": ["home_assistant"], "isolated": false}
  },
  "groups": {
    "research": {"namespaces": ["thinking", "docs", "web", "financial"]},
    "maxitrader": {"namespaces": ["thinking", "financial", "docs", "web", "trading"]}
  }
}
```

Groups are plain unions of namespaces. Listing an isolated namespace in a group includes it and logs a warning; the `isolated` flag only keeps a namespace off default surfaces. A legacy `!` prefix is accepted as a deprecated alias.

### Access

| Endpoint | Servers |
|----------|---------|
| `/sse` | `default` group: thinking + docs + web (resolved from groups first, then namespaces) |
| `/sse/memory` | memory namespace only (imo) |
| `/sse/docs` | docs namespace only |
| `/sse/trading` | trading (isolated) only |
| `/sse/home` | home namespace only |

imo (memory ns) is deliberately **not** on the default surface — clients need
`X-Namespace: memory` or `ops` (group over all namespaces) for `mcproxy_imo__*`
tools. Sessions snapshot their tool list at connect; config hot-reloads affect
new connections only.

Use `X-Namespace: dev` header to override endpoint.

## Project Structure

```
mcproxy/
├── main.py                  # Entry point
├── cli.py                   # CLI argument parsing
├── server/
│   ├── __init__.py          # FastAPI app factory
│   ├── admin_routes.py      # Admin API endpoints
│   ├── auth_middleware.py    # Auth middleware
│   ├── lifecycle.py         # Startup/shutdown lifecycle
│   ├── sse.py               # SSE endpoint handler
│   └── handlers/
│       ├── meta_tools.py    # Meta-tool definitions
│       ├── parsing.py       # Request parsing
│       ├── response.py      # Response formatting
│       └── tools/
│           ├── execute.py   # Execute handler
│           ├── search.py    # Search handler
│           ├── inspect.py   # Inspect handler
│           ├── help.py      # Help handler
│           └── router.py    # Tool routing
├── server_manager.py        # MCP server lifecycle management
├── http_backend.py          # HTTP MCP server connector
├── sandbox/
│   ├── executor.py          # Sandbox code execution
│   ├── runtime.py           # Runtime environment
│   ├── pool.py              # Sandbox instance pool
│   ├── proxy.py             # Tool proxy objects
│   ├── validation.py        # Code validation
│   ├── security.py          # Blocked imports/builtins
│   ├── access_control.py    # Namespace access control
│   └── constants.py         # Security constants
├── auth/
│   ├── agent_registry.py    # Agent CRUD + API keys
│   ├── credential_store.py  # Encrypted credential storage
│   ├── audit_logger.py      # Audit logging
│   └── scope_resolver.py    # Scope resolution
├── manifest/
│   ├── registry.py          # Capability registry
│   ├── query.py             # Manifest queries
│   ├── hooks.py             # Event hooks (config change, etc.)
│   ├── typescript_gen.py    # TypeScript type generation
│   └── errors.py            # Error types
├── blocklist.py             # Security blocklist
├── config_watcher.py        # Config loading + hot-reload
├── config_reloader.py       # Config reload handler
├── session_stash.py         # Per-session KV store with TTL
├── tool_aggregator.py       # Tool aggregation
├── code_validator.py        # Code pattern validation
├── adapter.py               # MCP adapter
├── api_parallel.py          # Parallel execution helper
├── api_stubs.py             # API stubs for sandbox
└── mcproxy.json             # Configuration
```

## Key Constraints

- **Python 3.11+** required
- **Port 12010** (hardcoded)
- **Memory**: <100MB target
- **Reload**: 1–2 seconds acceptable

## Memory & Tracking

**bd (beads) is retired.** Do not create or update bd issues. Store durable facts,
decisions, and infrastructure state in **imo** (the OKF-based memory MCP —
reachable through the live gateway's `memory` namespace / `mcproxy_imo__*` tools).
Ephemeral task lists stay in-conversation.

## References

- [docs/EXAMPLES.md](docs/EXAMPLES.md) - Detailed examples
- [docs/HISTORY.md](docs/HISTORY.md) - Archived features
- [ROADMAP.md](ROADMAP.md) - Future plans
- [README.md](README.md) - Overview

## Session Completion

Work is NOT complete until `git push` succeeds:

```bash
git pull --rebase && git push && git status  # must show "up to date with origin"
```

Persist anything a future session needs (follow-ups, decisions, live-state
changes) via imo before closing the session.
