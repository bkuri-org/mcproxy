---
id: note-007
tags: [roadmap]
created: 2026-05-10T09:32:56.738594+00:00
source: ROADMAP.md
---

# ROADMAP

# MCProxy v4.x Roadmap - The Ultimate MCP Gateway

> **Vision**: Build the most intelligent, performant, and reliable MCP gateway that anticipates agent needs, prevents abuse, and provides unmatched developer experience.

**Current Version**: v4.1.0 (Auth System ✅)  
**Next Release**: v4.2 (Security Hardening)  
**Ultimate Goal**: 100% Ultimate MCP Gateway by v5.0

---

## 📊 Overview

MCProxy v4.x introduces **51 carefully planned features** across **4 major releases** over **18 months**, transforming MCProxy from a simple gateway into an intelligent agent accelerator.

### Release Timeline

| Release | Timeline | Ultimate Status | Theme |
|---------|----------|-----------------|-------|
| **v4.0** | 10 weeks | 85% Ultimate | Intelligence + Performance |
| **v4.1** | +4 weeks | 88% Ultimate | Credential Broker |
| **v4.2** | +4 weeks | 92% Ultimate | Defense in Depth |
| **v4.3** | +4 weeks | 95% Ultimate | Advanced Optimization |
| **v5.0** | +8 weeks | 100% Ultimate | Scale + Accessibility |

---

## 🎯 v4.0: Intelligence + Performance (10 Weeks)

### Theme: "The Most Intelligent MCP Gateway"

**Ship Date**: Week 10  
**Deliverable**: Working cache, self-correcting tools, reasoning engine, production safety

### Phase 1: Alpha (Weeks 1-5) - Foundation

#### 🚀 Flagship Feature: Intelligent Caching System
**Task**: [MCPROXY-adt](https://github.com/your-repo/mcproxy/issues/MCPROXY-adt)

Multi-tier cache that dramatically accelerates agent workflows by avoiding redundant tool calls.

**Key Features**:
- **Multi-tier architecture**: Global cache (shared) + Session cache (isolated) + Namespace isolation
- **Smart TTL**: Per-namespace limits (trading: 60s, docs: 3600s) with agent override capability
- **Cache API**: `cache.clear()`, `cache.invalidate()`, `cache.stats()`
- **LRU eviction**: Automatic cleanup of least-recently-used entries
- **Memory backend**: Fast in-memory cache (Redis support in v4.1)

**Subtasks**:
- [MCPROXY-adt.1](https://github.com/your-repo/mcproxy/issues/MCPROXY-adt.1) - Cache Core (interface + basic operations)
- [MCPROXY-adt.2](https://github.com/your-repo/mcproxy/issues/MCPROXY-adt.2) - Multi-Tier Architecture
- [MCPROXY-adt.3](https://github.com/your-repo/mcproxy/issues/MCPROXY-adt.3) - TTL Management
- [MCPROXY-adt.4](https://github.com/your-repo/mcproxy/issues/MCPROXY-adt.4) - Cache Management API + LRU

**Impact**: 50% reduction in tool calls, 3x faster workflows

#### 🔍 Simplified Search
**Task**: [MCPROXY-auk](https://github.com/your-repo/mcproxy/issues/MCPROXY-auk)

Remove depth levels, make search behavior automatic based on query.

**Changes**:
- Empty query → Server list with tool counts
- Query provided → Matching tools with descriptions
- Fewer parameters = less confusion

**Impact**: Simpler API, better discoverability

#### 🧠 Self-Correcting Tool Usage

**Enhanced Search with Schemas** - [MCPROXY-7v0](https://github.com/your-repo/mcproxy/issues/MCPROXY-7v0)
- Include `inputSchema` and auto-generated usage examples in search results
- Agents see exactly how to call tools without trial-and-error

**Fuzzy Tool Name Matching** - [MCPROXY-29c](https://github.com/your-repo/mcproxy/issues/MCPROXY-29c)
- Suggest correct tool names on errors using Levenshtein distance
- "Tool 'serch' not found. Did you mean 'search'?"

**Fuzzy Search + Auto-Inspect** - [MCPROXY-ibi](https://github.com/your-repo/mcproxy/issues/MCPROXY-ibi)
- Auto-inspect tool schema when calls fail
- Self-correcting behavior without agent intervention

**Impact**: 60% reduction in tool call errors

#### 💭 Built-in Reasoning Engine
**Task**: [MCPROXY-5vi](https://github.com/your-repo/mcproxy/issues/MCPROXY-5vi)

Agents think before acting on dangerous operations.

**Features**:
- `think=True` parameter triggers reasoning before execution
- Configurable engine: `sequential_thinking`, `think_tool`, `atom_of_thoughts`
- Auto-trigger on dangerous operations (delete, drop, production)
- Override with `think=False` for trusted operations

**Impact**: 80% reduction in dangerous operation errors

---

### Phase 2: Beta (Weeks 6-8) - Safety & Reliability

#### 🛡️ Resource Management

**Budget System** - [MCPROXY-5is](https://github.com/your-repo/mcproxy/issues/MCPROXY-5is)
- Multi-dimensional budgets: tool calls, think calls, execution time, cache entries
- Prevent runaway agents from exhausting resources
- Per-namespace limits: `namespaces.trading.max_tool_calls = 500`

**Cache Backend Abstraction** - [MCPROXY-wr8](https://github.com/your-repo/mcproxy/issues/MCPROXY-wr8)
- Pluggable cache interface: memory/Redis/file backends
- Switch backends via config without code changes

#### 📊 Observability

**Tool Health Tracking** - [MCPROXY-173](https://github.com/your-repo/mcproxy/issues/MCPROXY-173)
- Track success rate, latency percentiles (p50/p95/p99), last errors
- Know what's failing before agents complain

**Distributed Tracing** - [MCPROXY-e6p](https://github.com/your-repo/mcproxy/issues/MCPROXY-e6p)
- Trace requests across multiple tool calls with timing and dependencies
- Export to OpenTelemetry (Jaeger/Zipkin)
- 10x faster debugging of complex workflows

**Prometheus Metrics** - [MCPROXY-dok](https://github.com/your-repo/mcproxy/issues/MCPROXY-dok)
- Export standard metrics: cache hit rate, tool latency, errors
- Integrate with existing monitoring stacks
- `/metrics` endpoint in Prometheus format

#### ⚡ Safety Controls

**Rate Limiting** - [MCPROXY-p2p](https://github.com/your-repo/mcproxy/issues/MCPROXY-p2p)
- Token bucket rate limits per tool/namespace
- Strategies: queue, reject, retry with exponential backoff
- Protect external APIs from abuse

**Circuit Breakers** - [MCPROXY-c43](https://github.com/your-repo/mcproxy/issues/MCPROXY-c43)
- Auto-detect failing services
- Temporarily stop routing requests to prevent cascading failures
- Auto-recovery when service heals

**Audit Trail** - [MCPROXY-bu9](https://github.com/your-repo/mcproxy/issues/MCPROXY-bu9)
- Structured logging with user/reason/ticket metadata
- Compliance-ready audit logs
- Search logs: `audit.search(user='alice', start='2025-03-01')`

---

### Phase 3: RC (Weeks 9-10) - Integration & Polish

#### 🚀 Performance Optimization

**Predictive Prefetching** - [MCPROXY-0lu](https://github.com/your-repo/mcproxy/issues/MCPROXY-0lu) ⭐ KILLER FEATURE
- Analyze tool call patterns to prefetch likely-needed data
- After calling tool A, prefetch B with 80% confidence
- **Impact**: 50% latency reduction, agents feel instantaneous

**Batch API** - [MCPROXY-d4n](https://github.com/your-repo/mcproxy/issues/MCPROXY-d4n)
- Combine multiple tool calls into single request
- 3-5x faster for batch operations
- Reduces network overhead

**Connection Pool + Cache Integration** - [MCPROXY-8s0](https://github.com/your-repo/mcproxy/issues/MCPROXY-8s0)
- Size connection pool dynamically based on cache hit rate
- High cache hit rate → smaller pool (cache serves requests)
- TTL-informed connection lifetime

#### 🎨 Developer Experience

**Dry Run + Validation** - [MCPROXY-aku](https://github.com/your-repo/mcproxy/issues/MCPROXY-aku)
- Preview execution plan without running: `dry_run=True`
- Validate syntax, schema, permissions before executing
- Combine with think: `dry_run=True, think=True`

**Auto-Think Budget Awareness** - [MCPROXY-62e](https://github.com/your-repo/mcproxy/issues/MCPROXY-62e)
- Skip auto-think when budget is low
- Preserve think calls for critical operations

**Parameter Normalization** - [MCPROXY-afd](https://github.com/your-repo/mcproxy/issues/MCPROXY-afd)
- Accept both `snake_case` and `camelCase` parameters
- Auto-normalize to schema's expected format
- Reduces parameter name errors

---

## 🔐 v4.1: Auth System (✅ COMPLETE)

### Theme: "Credential Broker"

**Ship Date**: Week 14 (COMPLETE)
**Deliverable**: JWT authentication, credential injection, scope-based access

### Security

**JWT-Based Agent Authentication** - [MCPROXY-yyc]
- OAuth 2.0 client credentials flow
- Agents receive JWT with scopes, never see actual API keys
- Scope-based access control with fallback chain

**Encrypted Credential Store** - [MCPROXY-eoy]
- AES-256-GCM encrypted SQLite storage
- Credentials injected at tool execution time
- Per-service, per-permission credential mapping

**Agent Registry** - [MCPROXY-4qn]
- Agent client management with bcrypt hashing
- Secret rotation support
- Enable/disable agents

**Scope Resolver** - [MCPROXY-sna]
- Scope → credential mapping
- Fallback chain: `service:permission` → `service:default`
- Tool-specific scope requirements

### Extensibility

**Plugin System** - [MCPROXY-1ya](https://github.com/your-repo/mcproxy/issues/MCPROXY-1ya)
- Custom middleware, transforms, extensions
- Plugin hooks: `before_tool_call`, `after_tool_call`, `on_error`
- Community contributions without modifying core

**Tool Composition** - [MCPROXY-9jd](https://github.com/your-repo/mcproxy/issues/MCPROXY-9jd)
- Define reusable tool chains (macros)
- Example: `fetch → transform → store` as single operation
- DRY principle for workflows

**Webhook/Event System** - [MCPROXY-3h9](https://github.com/your-repo/mcproxy/issues/MCPROXY-3h9)
- Push notifications when tools complete/fail
- Real-time updates without polling
- HMAC-SHA256 signatures for security

---

## 🛡️ v4.2: Security Hardening (4 Weeks)

### Theme: "Defense in Depth"

**Ship Date**: Week 18
**Deliverable**: Blocklist system, container isolation, server capability registry

### Online Blocklist System

**Blocklist Infrastructure** - [MCPROXY-blk]
- GitHub-hosted `blocklist.json` with periodic sync
- Local cache with offline fallback
- Configurable sync interval (default: 1 hour)
- Startup validation against cached blocklist

**Server Classification** - [MCPROXY-cls]
- Tier system: `safe`, `network`, `secret`, `risky`
- Blocked servers: Hard block at startup with error
- Risky servers: Require explicit acknowledgment in config
- Unclassified servers: Warning with recommendation to classify

**Blocklist Schema**:
```json
{
  "blocked": {
    "@executeautomation/tmux-mcp-server": {
      "reasons": ["arbitrary_shell_execution", "host_filesystem_access", "credential_exposure"],
      "severity": "critical"
    }
  },
  "risky": {
    "@executeautomation/playwright-mcp-server": {
      "reasons": ["browser_automation", "host_filesystem_access"],
      "severity": "high",
      "requires_ack": true
    }
  }
}
```

### Container Hardening

**Shell Removal** - [MCPROXY-shl]
- Alias `sh`, `bash`, `python` to `/dev/null` in container
- Only `uv` and `node` available for MCP servers
- Prevents arbitrary code execution

**Filesystem Isolation** - [MCPROXY-fs]
- Read-only root filesystem where possible
- Credentials passed via systemd `Environment=` only (no `.env` mount)
- `/srv` not accessible from container
- `ProtectHome=true`, `ProtectSystem=strict`

**Capability Dropping** - [MCPROXY-cap]
- `NoNewPrivileges=true`
- `CapDrop=ALL`
- `PrivateTmp=true`
- Network isolation (remove `Network=host`)

### Configuration Schema

```json
{
  "security": {
    "blocklist_url": "https://raw.githubusercontent.com/mcproxy/blocklist/main/blocklist.json",
    "blocklist_sync_interval": 3600,
    "allow_risky_servers": false,
    "risky_server_acknowledgments": {
      "playwright": "Required for browser automation testing"
    }
  }
}
```

### Success Metrics
- **Blocked servers detected**: 100% at startup
- **Container escape attempts**: 0 successful
- **Credential exposure incidents**: 0
- **False positive rate**: <1%

---

## 🛡️ v4.2: Security Hardening (4 Weeks)

### Theme: "Defense in Depth"

**Ship Date**: Week 22  
**Deliverable**: Online blocklist system, container isolation, server capability registry

### Online Blocklist System

**Blocklist Infrastructure** - [MCPROXY-blk]
- GitHub-hosted `blocklist.json` with periodic sync
- Local cache with offline fallback to embedded blocklist
- Configurable sync interval (default: 1 hour)
- Startup validation against cached blocklist
- Hot reload: Re-check on config changes
- Manual refresh: `POST /admin/blocklist/refresh`

**Server Classification** - [MCPROXY-cls]
- Tier system: `safe`, `network`, `secret`, `risky`
- Blocked servers: Hard block at startup with error message
- Risky servers: Require explicit acknowledgment in config
- Unclassified servers: Warning with recommendation to classify

**Blocklist Schema**:
```json
{
  "version": "2024.01.15",
  "updated": "2024-01-15T12:00:00Z",
  "blocked": {
    "@executeautomation/tmux-mcp-server": {
      "reasons": ["arbitrary_shell_execution", "host_filesystem_access", "credential_exposure"],
      "severity": "critical"
    }
  },
  "risky": {
    "@executeautomation/playwright-mcp-server": {
      "reasons": ["browser_automation", "host_filesystem_access"],
      "severity": "high",
      "requires_ack": true
    }
  }
}
```

### Container Hardening

**Shell Removal** - [MCPROXY-shl]
- Alias `sh`, `bash`, `python` to `/dev/null` in container PATH
- Only `uv` and `node` available for MCP servers
- Prevents arbitrary code execution via shell
- MCP servers needing Python (jesse, coinmarketcap) use venv python directly

**Filesystem Isolation** - [MCPROXY-fs]
- Read-only root filesystem where possible
- Credentials passed via systemd `Environment=` only (no `.env` mount)
- `/srv` not accessible from container
- `ProtectHome=true`, `ProtectSystem=strict`
- Mount `/srv/containers/mcproxy/config` read-only
- Mount `/srv/containers/mcproxy/data` for credentials DB (encrypted)

**Capability Dropping** - [MCPROXY-cap]
- `NoNewPrivileges=true`
- `CapDrop=ALL`
- `PrivateTmp=true`
- Network isolation (remove `Network=host`, use bridge networking)

### Configuration Schema
```json
{
  "security": {
    "blocklist_enabled": true,
    "blocklist_url": "https://raw.githubusercontent.com/mcproxy/blocklist/main/blocklist.json",
    "blocklist_sync_interval": 3600,
    "allow_risky_servers": false,
    "risky_server_acknowledgments": {
      "playwright": "Required for browser automation testing"
    }
  }
}
```

### Success Metrics
- **Blocked servers detected**: 100% at startup
- **Container escape attempts**: 0 successful
- **Credential exposure incidents**: 0
- **False positive rate**: <1%
- **Blocklist sync uptime**: 99.9%

---

## 🌟 v4.3: Advanced Optimization (4 Weeks)

### Theme: "Fully Optimized"

**Ship Date**: Week 26  
**Deliverable**: Self-optimizing cache, auto-parallelization, advanced features

### Learning & Adaptation

**Adaptive TTL Learning** - [MCPROXY-s1j](https://github.com/your-repo/mcproxy/issues/MCPROXY-s1j)
- Track cache hits/misses/staleness to auto-adjust TTL
- Learning strategies: hit rate, staleness, cost
- Self-optimizing cache

**Adaptive Timeouts** - [MCPROXY-4g9](https://github.com/your-repo/mcproxy/issues/MCPROXY-4g9)
- Use historical data (avg + 2σ) for intelligent timeouts
- Reduce timeout errors by 60%

**Cache Invalidation** - [MCPROXY-fbz](https://github.com/your-repo/mcproxy/issues/MCPROXY-fbz)
- Auto-invalidate related entries on mutations
- Dependency tracking

### Advanced Features

**Auto-Parallelization** - [MCPROXY-f6t](https://github.com/your-repo/mcproxy/issues/MCPROXY-f6t)
- Static analysis to detect independent tool calls
- Execute concurrently automatically
- 40% time savings

**Session Variables** - [MCPROXY-ut5](https://github.com/your-repo/mcproxy/issues/MCPROXY-ut5)
- Cross-call state persistence: `session.set()`, `session.get()`
- Share state between tool calls

**Tool Versioning** - [MCPROXY-7ql](https://github.com/your-repo/mcproxy/issues/MCPROXY-7ql)
- Support multiple versions of same tool
- Gradual migration support

**Result Size Limits** - [MCPROXY-iko](https://github.com/your-repo/mcproxy/issues/MCPROXY-iko)
- Truncate large outputs with smart summarization
- Prevent context overflow

---

## 🔮 v5.0: Scale + Accessibility (8 Weeks)

### Theme: "Industry Standard"

**Ship Date**: Week 26  
**Deliverable**: 100% Ultimate MCP Gateway

### Scale
- Multi-region deployment
- Edge caching
- GraphQL API
- Cost tracking

### Accessibility
- Visual workflow builder
- AI-powered debugging
- Auto-documentation
- Collaborative workspaces

---

## 🏆 Killer Features (Differentiators)

### 1. Predictive Prefetching ⭐
**What**: Anticipates agent needs, prefetches data before requested  
**Impact**: 50% latency reduction  
**Uniqueness**: No other MCP proxy has this

### 2. Built-in Reasoning Engine
**What**: Agents think before acting on dangerous operations  
**Impact**: 80% error reduction  
**Uniqueness**: First proxy with built-in reasoning

### 3. Adaptive TTL Learning
**What**: Cache optimizes itself based on usage patterns  
**Impact**: 3x cache efficiency  
**Uniqueness**: Self-optimizing cache

### 4. Tool Composition
**What**: Define reusable workflows (macros)  
**Impact**: 50% less agent code  
**Uniqueness**: DRY principle for tools

### 5. Budget System
**What**: Prevents runaway agents  
**Impact**: 100% abuse prevention  
**Uniqueness**: First agent-aware budget system

---

## 📈 Performance Targets

### v4.0 Success Metrics
- **Cache hit rate**: >70%
- **Error rate**: <5% (down from 25%)
- **P95 latency**: <200ms (down from 500ms)
- **Agent abuse**: Zero incidents
- **Budget compliance**: 100%

### v4.1 Success Metrics
- **Secret management adoption**: 100% of production deployments
- **Community plugins**: 10+ available
- **Tool composition usage**: 50% of users
- **Security incidents**: Zero

### v4.2 Success Metrics (Security Hardening)
- **Blocked servers detected**: 100% at startup
- **Container escape attempts**: 0 successful
- **Credential exposure incidents**: 0
- **False positive rate**: <1%

### v4.3 Success Metrics
- **Cache efficiency**: >90%
- **Auto-parallelization savings**: >40% time
- **Adaptive timeout error reduction**: 60%
- **Session variable adoption**: 80% of workflows

---

## 🚦 Release Criteria

### v4.0-alpha Ready When
- ✅ Cache system working (all 4 subtasks complete)
- ✅ Fuzzy matching functional
- ✅ Think engine integrated
- ✅ Search simplified
- ✅ Basic tests passing

### v4.0-beta Ready When
- ✅ Budgets enforced
- ✅ Health tracking working
- ✅ Tracing functional
- ✅ Metrics exported
- ✅ Circuit breakers operational

### v4.0-rc Ready When
- ✅ Predictive prefetching working
- ✅ Batch API functional
- ✅ All P1/P2 features complete
- ✅ Documentation updated
- ✅ Load testing passed (>1000 concurrent sessions)

---

## 📊 Feature Breakdown

### By Priority
- **P1 (Foundation)**: 7 tasks (~4 weeks)
- **P2 (Core Features)**: 13 tasks (~13 weeks)
- **P3 (Optimization)**: 13 tasks (~13 weeks)
- **P4 (Advanced)**: 18 tasks (~13 weeks)

**Total**: 51 tasks

### By Category
- **Performance**: 8 features (cache, pool, prefetch, parallel, batch, streaming, timeouts, warming)
- **Intelligence**: 6 features (reasoning, learning, self-correction, NL API, composition, recommendations)
- **Safety**: 9 features (budgets, circuit breakers, rate limiting, auth, audit, dry run, validation)
- **Observability**: 4 features (tracing, health, metrics, audit)
- **Developer Experience**: 8 features (composition, mock, session vars, normalization, fuzzy matching, search)
- **Extensibility**: 3 features (plugins, webhooks, versioning)
- **Security**: 2 features (token auth, secret management)
- **Infrastructure**: 3 features (cache backend, connection pool, schema migration)

---

## 🎖️ Competitive Positioning

### vs. Direct MCP Proxies

| Feature | MCProxy v4.0 | Others |
|---------|--------------|--------|
| **Caching** | ✅ Multi-tier + Adaptive TTL | ❌ None |
| **Intelligence** | ✅ Reasoning + Learning | ❌ None |
| **Safety** | ✅ Budgets + Circuit Breakers | ❌ None |
| **Observability** | ✅ Tracing + Metrics | ⚠️ Basic |
| **Performance** | ✅ 10x faster | ⚠️ Baseline |
| **Self-Correction** | ✅ Fuzzy + Auto-Inspect | ❌ None |
| **Extensibility** | ✅ Plugins + Webhooks | ❌ None |

### vs. Generic API Gateways

| Feature | MCProxy v4.0 | Kong/NGINX |
|---------|--------------|------------|
| **MCP-Native** | ✅ Full protocol support | ❌ Protocol agnostic |
| **Tool Semantics** | ✅ Understands tools | ❌ Treats as endpoints |
| **Agent Optimization** | ✅ Budgets + Sessions | ❌ Generic |
| **Self-Healing** | ✅ Circuit breakers + Fallbacks | ⚠️ Basic |
| **Intelligence** | ✅ Reasoning + Learning | ❌ None |

---

## 💰 ROI Analysis

### v4.0 Investment vs. Return
**Investment**: 10 weeks development

**Returns**:
- 50% latency reduction (prefetching)
- 80% error reduction (reasoning + self-correction)
- 3x cache efficiency (adaptive TTL)
- 100% abuse prevention (budgets)
- 10x faster debugging (tracing)

### Business Impact
- **Developer Productivity**: 5x faster agent development
- **Operational Cost**: 50% reduction (caching + pooling)
- **Reliability**: 99.9% uptime (circuit breakers + fallbacks)
- **Security**: Enterprise-ready (audit + auth + secrets)

---

## 🛠️ Getting Started

### For Users

**Track Progress**:
```bash
# List all open tasks
bd list --status open --all

# View specific task details
bd show MCPROXY-adt

# Check what's ready to work on
bd ready
```

**Stay Updated**:
- Watch this repository for release announcements
- Check the [issues](https://github.com/your-repo/mcproxy/issues) for detailed task discussions
- Join discussions on specific features you're interested in

### For Contributors

**Pick a Task**:
```bash
# Find unblocked work
bd ready

# Claim a task
bd claim MCPROXY-adt.1

# View dependencies
bd show MCPROXY-8s0  # Shows what blocks it
```

**Implementation Process**:
1. Read task description and acceptance criteria
2. Check dependencies are complete
3. Implement feature
4. Write tests
5. Update documentation
6. Submit PR with task ID in title

---

## 📅 Milestone Timeline

```
Week 1-2:   [v4.0-alpha] Planning + Cache Core
Week 3-5:   [v4.0-alpha] Self-Correction + Reasoning
Week 6-8:   [v4.0-beta]  Safety + Observability
Week 9-10:  [v4.0-rc]    Performance + Polish
            ───────────── v4.0 RELEASE ─────────────
Week 11-14: [v4.1]       Auth System (COMPLETE)
            ───────────── v4.1 RELEASE ─────────────
Week 15-18: [v4.2]       Security Hardening
Week 19-22: [v4.2]       Container Isolation
            ───────────── v4.2 RELEASE ─────────────
Week 23-26: [v4.3]       Advanced Optimization
            ───────────── v4.3 RELEASE ─────────────
Week 27-34: [v5.0]       Scale + Accessibility
            ───────────── v5.0 RELEASE ─────────────
```

---

## 🎯 Vision Statement

**By v5.0, MCProxy will be the definitive MCP platform - the only choice for teams building production AI agents.**

With intelligent caching that learns, reasoning that prevents errors, budgets that prevent abuse, and extensibility that enables ecosystems, MCProxy v4.x isn't just a gateway - it's an **agent accelerator**.

**10x faster agents. 80% fewer errors. 100% reliability.**

That's the promise of MCProxy v4.x.

---

## 📞 Contact & Support

- **Issues**: [GitHub Issues](https://github.com/your-repo/mcproxy/issues)
- **Discussions**: [GitHub Discussions](https://github.com/your-repo/mcproxy/discussions)
- **Documentation**: [docs/](./docs/)
- **Examples**: [examples/](./examples/)

---

## 📜 Changelog

### v4.1 (Target: Week 14)
- JWT-Based Agent Authentication
- Encrypted Credential Store
- Agent Registry
- Scope Resolver

### v4.2 (Target: Week 22)
- Online Blocklist System
- Server Classification
- Container Hardening (Shell Removal)
- Filesystem Isolation
- Capability Dropping

### v4.0-alpha (Target: Week 5)
- Intelligent Caching System
- Simplified Search
- Enhanced Search with Schemas
- Fuzzy Tool Name Matching
- Built-in Reasoning Engine

### v4.0-beta (Target: Week 8)
- Budget System
- Tool Health Tracking
- Distributed Tracing
- Prometheus Metrics
- Rate Limiting
- Circuit Breakers
- Audit Trail

### v4.0-rc (Target: Week 10)
- Predictive Prefetching
- Batch API
- Connection Pool Integration
- Dry Run + Validation
- Auto-Think Budget Awareness
- Parameter Normalization

---

**Last Updated**: 2026-03-21  
**Next Review**: Weekly during active development

---

*This roadmap is a living document. Priorities may shift based on user feedback, technical discoveries, and market needs. Star this repository to stay updated!*

