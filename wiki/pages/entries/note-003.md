---
id: note-003
tags: [examples]
created: 2026-05-10T09:32:52.305124+00:00
source: docs/EXAMPLES.md
---

# EXAMPLES

# MCProxy Examples

Detailed examples for using MCProxy. For quick reference, see [AGENTS.md](../AGENTS.md).

## Setup

```bash
# Setup with uv (recommended)
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Setup with pip (traditional)
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
# Development server
python main.py --log

# Custom config and port
python main.py --config mcproxy.json --port 12010

# As native MCP server (stdio mode)
python main.py --stdio --config mcproxy.json
```

## HTTP Testing

### Health Check
```bash
curl http://192.168.50.71:12010/health
```

### Tools List
```bash
# Default namespace
curl -X POST http://192.168.50.71:12010/sse \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# With namespace header
curl -X POST http://192.168.50.71:12010/sse \
  -H "Content-Type: application/json" \
  -H "X-Namespace: dev" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### SSE Streaming
```bash
# Default namespace
curl -N http://localhost:12010/sse

# Namespaced endpoint
curl -N http://localhost:12010/sse/dev
```

## Authentication

### Get Access Token
```bash
curl -X POST http://192.168.50.71:12010/oauth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET"
```

### Authenticated Request
```bash
TOKEN="your-jwt-token"
curl -X POST http://192.168.50.71:12010/sse \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Namespace: dev" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Code Mode API

Single tool: `mcproxy(action='execute'|'search'|'inspect', ...)`

### Execute Code
```python
# Basic usage
result = mcproxy(
    action='execute',
    code='api.server("wikipedia").search(query="python")',
    namespace='dev'
)

# Multi-line code
result = mcproxy(action='execute', code="""
    # Search for information
    data = api.server("wikipedia").search(query="python")
    
    # Process the result
    lines = data.split("\\n")
    return f"Found {len(lines)} lines"
""", namespace='dev')

# With retries
result = mcproxy(
    action='execute',
    code='api.server("api").call()',
    namespace='dev',
    retries=2
)
```

### Search Tools
```python
# Find all tools matching query
mcproxy(action='search', query='wikipedia', namespace='dev')

# Get all servers and tools
mcproxy(action='search', query='', namespace='dev')

# Deep search
mcproxy(action='search', query='file', namespace='dev', max_depth=2)
```

### Inspect Schema
```python
# Get tool schema
schema = mcproxy(
    action='inspect',
    server='wikipedia',
    tool='search'
)
# Returns: {name, description, inputSchema}
```

### Parallel Execution
```python
results = parallel([
    lambda: api.server('server1').tool1(arg1='val1'),
    lambda: api.server('server2').tool2(arg2='val2'),
])
# Returns: [{"status": "fulfilled", "result": ...}, ...]
```

### Response Handling
```python
result = mcproxy(action='execute', code='...', namespace='dev')

# Auto-unwrapped responses - no need for:
#   result['content'][0]['text']

# String - direct access
if isinstance(result, str):
    print(result)  # "search results..."

# List - direct iteration
if isinstance(result, list):
    for item in result:
        print(item)

# Dict - direct access
if isinstance(result, dict):
    print(result['key'])
```

## Container Operations

```bash
# Build container
docker build -t localhost/mcproxy:latest .

# Run container locally
docker run -d --name mcproxy \
  -p 12010:12010 \
  -v $(pwd)/config:/app/config:Z \
  -v $(pwd)/.env:/app/.env:ro \
  localhost/mcproxy:latest

# Deploy to server2
sudo cp mcproxy.container /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start mcproxy.service
sudo journalctl -u mcproxy.service -f

# Auto-deploy (after initial setup)
git push  # Hook handles rest
```

## Config Validation

```bash
# Validate JSON config
python -m json.tool mcproxy.json

# Check syntax only
python -c "import json; json.load(open('mcproxy.json'))"
```

## MCP Protocol Reference

### JSON-RPC Request
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

### Tools/Call Request
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "mcproxy",
    "arguments": {
      "action": "execute",
      "code": "1+1",
      "namespace": "dev"
    }
  }
}
```

### Response Format
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "2"
      }
    ]
  }
}
```

## Thinking Tools

### Sequential Thinking

Multi-step reasoning for complex problems:

```python
result = api.server("sequential_thinking").sequentialthinking(
    thought="First, let me analyze the problem step by step",
    thoughtNumber=1,
    totalThoughts=3,
    nextThoughtNeeded=True
)
```

### Think Tool

Simple thought processing:

```python
result = api.server("think_tool").think(
    thought="Let me reason through this problem..."
)
```

## Available Servers

Common servers available:

| Server | Purpose |
|--------|---------|
| `wikipedia` | Wikipedia search and articles |
| `llms_txt` | Documentation access |
| `sequential_thinking` | Multi-step reasoning |
| `think_tool` | Simple thought processing |
| `fear_greed_index` | Market sentiment |
| `coinstats` | Cryptocurrency data |
| `youtube` | YouTube video search |

**Note**: Server availability varies by namespace. Run `api.manifest()` to see available servers.

