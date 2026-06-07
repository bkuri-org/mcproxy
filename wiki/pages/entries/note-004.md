---
id: note-004
tags: [history]
created: 2026-05-10T09:32:53.429550+00:00
source: docs/HISTORY.md
---

# HISTORY

# MCProxy History

Archived documentation from earlier versions and projects.

## Current Documentation

- [AGENTS.md](../AGENTS.md) - Agent guidelines (current)
- [ROADMAP.md](../ROADMAP.md) - Future plans and milestones
- [docs/EXAMPLES.md](EXAMPLES.md) - Usage examples

---

## Archived from MCProxy v3.x

### Agent Setup (v3.x)

Add this to your `CLAUDE.md` or `AGENTS.md`:

```markdown
## MCProxy Integration

MCProxy provides MCP tools via the v2 Code Mode API.

**⚠️ CRITICAL: Use only servers from the MCP instructions**

Server names vary by environment. Check the "Available servers and tools" section.

### Direct Execution (Recommended)

result = api.server("wikipedia").search(query="python")
```

### Hot-Reload Behavior

MCProxy supports **hot-reload** for configuration changes:

- ✅ Add/remove servers - Automatically starts/stops
- ✅ Add/remove namespaces - Endpoints update immediately
- ✅ Modify server configs - Servers restart automatically

Just edit `mcproxy.json` and save. Changes apply within 1 second!

### Code Mode API (v2)

MCProxy v2 uses a **Code Mode API** with just 2 meta-tools:

- **`search`** - Discover available tools
- **`execute`** - Run Python code with tool access via `api` object

This approach reduces context from ~15,000 tokens (76 tools) to ~1,000 tokens.

### Available Servers (v3.x)

- **perplexity_sonar** - Web search with recency filtering
- **wikipedia** - Wikipedia search and articles
- **playwright** - Browser automation (blocked by default in v4.2)
- **sequential_thinking** - Multi-step reasoning
- **think_tool** - Simple thought processing
- **fear_greed_index** - Market sentiment
- **coinstats** - Cryptocurrency data
- **youtube** - YouTube video search
- **llms_txt** - Documentation access
- **pure_md** - Markdown operations
- **tmux** - Shell commands (blocked by default in v4.2)

---

## Archived from Jesse-MCP Integration

These documents were created during the Jesse-MCP debugging project (Feb 2026).

### Quick Start

1. Start here: `docs/requirements/START_HERE_MCPROXY.txt`
2. Full context: `docs/requirements/INDEX.md`
3. Quick reference: `docs/requirements/MCPROXY_SESSION_CONTEXT.md`

### Key Files (Historical)

**Primary:** `/srv/containers/mcproxy/server.py`
- Debug logging in `handle_tools_call()` function
- Subprocess communication verification

**Secondary:** `/srv/containers/mcproxy/main.py`
- Environment variable passing

---

## Archived Analysis Documents

These contain design decisions and analysis from various projects:

### Performance & Optimization

- `docs/optimization_analysis.md` - Performance analysis
- `docs/performance_summary.md` - Performance findings
- `docs/empty_search_optimization.md` - Search optimization
- `docs/instruction_optimization_summary.md` - Instruction optimization

### Session & Stash

- `docs/session_summary.md` - Session management
- `docs/analysis/session_stash_design.md` - Stash design

### Tool Design

- `docs/final_tool_design.md` - Final tool architecture
- `docs/empty_search_optimization.md` - Search tool design
- `docs/search_config.md` - Search configuration

### Analysis

- `docs/analysis/EXECUTIVE_SUMMARY.md` - Executive overview
- `docs/analysis/IMPLEMENTATION_SNIPPETS.md` - Implementation code
- `docs/analysis/README.md` - Analysis index

---

## Migration Notes

### v1.x to v2.0

| v1.x | v2.0 |
|------|------|
| `servers` array | `servers` object (keyed by name) |
| Flat tool list | Namespaced tools |
| Direct tool calls | `search` + `execute` meta-tools |

### v3.x to v4.x

- Blocklist system added (v4.2)
- JWT authentication added (v4.1)
- Shell removal in container (v4.2)

---

## See Also

- [ROADMAP.md](../ROADMAP.md) - Future plans (detailed)
- [CHANGELOG.md](../CHANGELOG.md) - Version history
- [docs/EXAMPLES.md](EXAMPLES.md) - Current examples

## Archived Files

These files were archived (content now in ROADMAP.md):
- `UPCOMING_FEATURES.md` - Superseded by ROADMAP.md

