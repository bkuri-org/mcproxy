---
id: note-008
tags: [agents]
created: 2026-05-10T09:32:57.822852+00:00
source: AGENTS.md
---

# AGENTS

# MCProxy - Agent Guidelines

> **Status**: v4.2 - Security Hardening  
> **MCProxy** is a lightweight MCP gateway that aggregates multiple stdio MCP servers through namespaced SSE endpoints.

## Quick Reference

### IMPORTANT: MCP Protocol

MCProxy uses MCP (JSON-RPC 2.0), **not REST**:

| Endpoint | Type | Purpose |
|----------|------|---------|
| `GET /health` | REST | Health check |
| `POST /sse` | MCP | Main endpoint |
| `POST /sse/{namespace}` | MCP | Namespaced endpoint |

### Code Mode

Single tool: `mcproxy(action='execute'|'search'|'inspect', ...)`

```python
# Execute
mcproxy(action='execute', code='api.server("wikipedia").search(query="python")', namespace='dev')

# Search
mcproxy(action='search', query='wikipedia')

# Inspect
mcproxy(action='inspect', server='wikipedia', tool='search')
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

### Usage

```python
# Execute code
result = mcproxy(action='execute', code='...', namespace='dev')

# Search tools
results = mcproxy(action='search', query='wikipedia')

# Inspect schema
schema = mcproxy(action='inspect', server='wikipedia', tool='search')
```

### Parallel Execution

```python
results = parallel([
    lambda: api.server('s1').tool1(),
    lambda: api.server('s2').tool2(),
])
```

### Response Types

Auto-unwrapped - no need for `result['content'][0]['text']`:
- **String**: Direct file contents, messages
- **List**: Direct iteration
- **Dict**: Direct key access

## Authentication (v4.2)

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

# Register - automatically generates API key
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
GET  /admin/agents              List all agents
GET  /admin/agents/{id}        Get agent details  
GET  /admin/agents/{id}/api-key    Check if API key exists
POST /admin/agents/{id}/api-key   Generate/rotate API key
DELETE /admin/agents/{id}/api-key  Revoke API key
POST /admin/agents/{id}/rotate  Rotate secret
POST /admin/agents/{id}/enable  Enable agent
POST /admin/agents/{id}/disable Disable agent
DELETE /admin/agents/{id}       Delete agent
```

### Authentication

Requests from **localhost** (127.0.0.1) are automatically authorized when `MCPROXY_ADMIN_KEY` is not set.

For remote access, use the `X-Admin-Key` header with the configured admin key:

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

- `MCPROXY_ADMIN_KEY` - The admin key (required for production)
- `auth.admin_key_env` - Config option to customize env var name
- `auth.rotate_reauth` - Config option to require re-auth after rotation

## Security Hardening (v4.2)

Defense-in-depth with blocklist validation and container hardening.

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

### For Agents

- **Shell access disabled** - No sh/bash/python in sandbox
- **Blocklist enforced** - MCProxy exits if blocked servers detected
- **Credentials isolated** - API keys injected at execution, never exposed
- **Namespace isolation** - Access limited to assigned namespace

## Namespaces

### Configuration

```json
{
  "namespaces": {
    "dev": {"servers": ["wikipedia", "llms_txt"], "isolated": false},
    "home": {"servers": ["home_assistant"], "isolated": true}
  },
  "groups": {
    "full": {"namespaces": ["dev", "docs"]}
  }
}
```

### Access

| Endpoint | Servers |
|----------|---------|
| `/sse` | Unnamespaced + non-isolated |
| `/sse/dev` | dev namespace only |
| `/sse/home` | home (isolated) only |

Use `X-Namespace: dev` header to override endpoint.

## Project Structure

```
mcproxy/
├── main.py              # Entry point
├── server.py            # FastAPI SSE
├── server_manager.py    # MCP server management
├── config_watcher.py    # Config loading
├── blocklist.py         # Security blocklist
├── auth/                # Authentication
│   ├── credential_store.py
│   ├── jwt_keys.py
│   └── agent_registry.py
└── mcproxy.json         # Configuration
```

## Key Constraints

- **Python 3.11+** required
- **Port 12010** (hardcoded)
- **Memory**: <100MB target
- **Reload**: 1-2 seconds acceptable

## Issue Tracking

Uses **bd (beads)**:

```bash
bd ready              # Find unblocked work
bd create "Title"     # Create issue
bd close <id>        # Complete work
bd dolt push         # Push to remote
```

## References

- [docs/EXAMPLES.md](docs/EXAMPLES.md) - Detailed examples
- [docs/HISTORY.md](docs/HISTORY.md) - Archived features
- [ROADMAP.md](ROADMAP.md) - Future plans
- [README.md](README.md) - Overview

