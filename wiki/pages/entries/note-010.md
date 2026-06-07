---
id: note-010
tags: [infrastructure, deployment, config, reference]
created: 2026-05-13T14:57:00.000000+00:00
source: agent-memory-ingest
---

# Infrastructure Facts

> MCProxy deployment and runtime infrastructure reference. Auto-ingested from project configuration and agent memory.

---

## Service Endpoints

| Parameter | Value |
|-----------|-------|
| **Host** | `192.168.50.71` |
| **Port** | `12010` |
| **Protocol** | MCP (JSON-RPC 2.0) over SSE |
| **Health Check** | `GET http://192.168.50.71:12010/health` |
| **Main MCP Endpoint** | `POST http://192.168.50.71:12010/sse` |
| **Namespaced Endpoint** | `POST http://192.168.50.71:12010/sse/{namespace}` |

---

## Version & Runtime

| Parameter | Value |
|-----------|-------|
| **Installed Version** | 5.1.0 |
| **Python** | 3.11+ |
| **License** | MIT |
| **Author** | Bkuri (bk@kuri.casa) |
| **GitHub** | https://github.com/bkuri/mcproxy |

---

## Deployment

- **Method**: Quadlet (systemd podman unit)
- **Service**: `mcproxy.service`
- **Config File**: `mcproxy.json` (mounted from host)
- **Hot-Reload**: Enabled by default; add/remove servers without dropping connections
- **Auto-Deploy**: `git push` triggers hook

```bash
# Service management
sudo systemctl enable --now mcproxy.service
journalctl -u mcproxy.service -f

# Docker alternative
docker run -d -p 12010:12010 \
  -v $(pwd)/config:/app/config:Z \
  localhost/mcproxy:latest
```

---

## Configured MCP Servers (16)

### Financial & Market Data
| Server | Type | Command/URL |
|--------|------|-------------|
| `fear_greed_index` | stdio | `npx mcp-server-fear-greed` |
| `coinstats` | stdio | `npx @coinstats/coinstats-mcp` |
| `asset_price` | stdio | `npx asset-price-mcp` |
| `coinmarketcap` | stdio | Python `coin_api_mcp` module |

### Thinking & Reasoning
| Server | Type | Command/URL |
|--------|------|-------------|
| `sequential_thinking` | stdio | `npx @modelcontextprotocol/server-sequential-thinking` |
| `atom_of_thoughts` | stdio | Node `/tmp/MCP_Atom_of_Thoughts/build/index.js` |
| `think_tool` | stdio | `npx think-tool-mcp` |

### Documentation & Web
| Server | Type | Command/URL |
|--------|------|-------------|
| `llms_txt` | stdio | `uvx mcpdoc` (LangGraph docs) |
| `wikipedia` | stdio | `npx wikipedia-mcp` |
| `pure_md` | stdio | `npx puremd-mcp` |
| `perplexity_sonar` | stdio | `uvx perplexity-mcp` (model: sonar) |
| `youtube` | stdio | `npx @anaisbetts/mcp-youtube` |

### Automation
| Server | Type | Command/URL |
|--------|------|-------------|
| `tmux` | stdio | `npx @executeautomation/tmux-mcp-server` |
| `playwright` | stdio | `npx @executeautomation/playwright-mcp-server` |

### Trading & Home
| Server | Type | Command/URL |
|--------|------|-------------|
| `jesse` | HTTP | `http://localhost:12011/mcp` → proxies to `http://localhost:9100` |
| `home_assistant` | stdio | `npx @coolver/home-assistant-mcp@latest` |
| `zilliqa_insights` | HTTP | `https://insights.mcp.zilliqa.com/mcp` |

---

## Namespaces

| Namespace | Servers | Isolated |
|-----------|---------|----------|
| `thinking` | sequential_thinking, atom_of_thoughts, think_tool | No |
| `financial` | fear_greed_index, coinstats, asset_price, coinmarketcap | No |
| `docs` | llms_txt, wikipedia | No |
| `web` | pure_md, perplexity_sonar, youtube | No |
| `automation` | tmux, playwright | No |
| `trading` | jesse | **Yes** |
| `home` | home_assistant | No |
| `blockchain` | zilliqa_insights | No |

## Groups

| Group | Namespaces |
|-------|------------|
| `dev` | thinking, docs |
| `dev_full` | thinking, docs, web |
| `research` | thinking, docs, web, financial, blockchain |
| `automation_full` | automation, web |
| `everything` | thinking, financial, docs, web, automation, blockchain, **!trading** |
| `maxitrader` | thinking, financial, docs, web, blockchain, **!trading** |
| `normal` | automation, docs, home, thinking, web |

> The `!` prefix forces inclusion of an isolated namespace into a group.

---

## Jesse MCP Server Details

| Parameter | Value |
|-----------|-------|
| **MCP URL** | `http://localhost:12011/mcp` |
| **Jesse Backend** | `http://localhost:9100` |
| **Password** | Configured (env: `JESSE_PASSWORD`) |
| **Python Path** | `/srv/containers/jesse` + `/srv/containers/jesse-baremetal/venv/lib/python3.11/site-packages` |
| **LLM Endpoint** | `https://api.perplexity.ai` |
| **Default Timeout** | 350s |

### Tool-Specific Timeouts

| Tool | Timeout |
|------|---------|
| `backtest` | 900s (15 min) |
| `optimize` | 1200s (20 min) |
| `walk_forward` | 1800s (30 min) |

---

## Home Assistant Integration

| Parameter | Value |
|-----------|-------|
| **Agent URL** | `http://192.168.50.99:8099` |
| **Agent Key** | Configured (env: `HA_AGENT_KEY`) |

---

## Sandbox Configuration

| Parameter | Value |
|-----------|-------|
| **Execution Timeout** | 900s (15 min) |
| **Pool Size** | 3 (max 10) |
| **Pool Idle Timeout** | 300s (5 min) |

## Search Configuration

| Parameter | Value |
|-----------|-------|
| **Min Query Words** | 2 |
| **Max Tools Returned** | 5 |
| **Cache TTL** | 300s (5 min) |

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

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `MCPROXY_ADMIN_KEY` | Admin API key (required for production) |
| `COINSTATS_API_KEY` | CoinStats market data |
| `PUREMD_API_KEY` | PureMD web scraping |
| `PERPLEXITY_API_KEY` | Perplexity search (also used by Jesse LLM) |
| `CMC_API_KEY` | CoinMarketCap data |
| `HA_AGENT_KEY` | Home Assistant agent key |
