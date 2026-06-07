# MCProxy v4.2 Security Hardening Plan

**Created:** 2025-05-15
**Umbrella issue:** `mcproxy-8a4`
**Status:** Planning complete, awaiting execution

## Context

Full security audit revealed mcproxy runs as a **bare systemd service on server2** with zero container isolation. The existing Quadlet, Dockerfile, and hardened container configs exist in the repo but are completely unused. The audit identified 8 attack vectors across sandbox escape, network surface, credential exposure, container escape, dependencies, file access, and session isolation.

## Issue Hierarchy

```
mcproxy-8a4 [P0 epic] v4.2 Security Hardening — Defense in Depth (rescoped)
├── mcproxy-dly [P0] Phase 1: Containerize mcproxy with auto-deploy from main
│     └── blocks → mcproxy-5a1
├── mcproxy-5a1 [P1] Phase 2: Container security hardening (systemd + Podman)
├── mcproxy-3be [P0] Phase 3: Sandbox runtime hardening — close AST-only gap
├── mcproxy-7b1 [P1] Phase 4: Network and auth hardening
├── mcproxy-s14 [P2] Phase 5: Close blocklist validation gaps
└── mcproxy-fe3 [P3] Phase 6: Dependency cleanup and hardening
```

## Execution Order

```
Phase 1 (container) ──blocks──→ Phase 2 (container hardening)
Phase 3 (sandbox)     ← independent, can start immediately (code-only)
Phase 4 (network/auth) ← independent, can start immediately (code-only)
Phase 5 (blocklist)   ← after Phase 3 (shares security.py)
Phase 6 (deps)        ← can run anytime, lowest priority
```

**Recommended parallel start:** Phase 1 + Phase 3 + Phase 4 simultaneously.
Phase 1 is infra work (server2), Phase 3 and 4 are code-only (repo).

## Per-Phase Summary

### Phase 1: Containerize mcproxy [P0] `mcproxy-dly`
- Move from bare systemd to Quadlet container
- Bridge networking (NOT host), port 12010
- Push-to-deploy: `git push main` → image build → container restart
- Persistent volumes for data/cache, read-only config mount
- No .env file — credentials via systemd Environment= or Podman secrets
- Dev workflow preserved: pull → rebuild → restart

### Phase 2: Container Hardening [P1] `mcproxy-5a1`
- ReadOnlyRootfs, ProtectHome, ProtectSystem=strict, PrivateTmp
- NoNewPrivileges, CapDrop=ALL
- Memory limits (512MB)
- Shell removal verification (sh/bash/python → stubs)
- Align or remove podman-compose.yml (currently has host networking)

### Phase 3: Sandbox Runtime Hardening [P0] `mcproxy-3be`
- **Highest-risk code vulnerability.** AST-only enforcement means full `__builtins__` at runtime.
- Whitelist `__builtins__` — only safe functions, remove open/eval/exec/getattr/etc.
- Remove `sys` and `ast` from sandbox `local_vars`
- Runtime dunder attribute guards (not just AST-level)
- Dynamic string detection for obfuscated attribute access
- IPC socket protection

### Phase 4: Network & Auth Hardening [P1] `mcproxy-7b1`
- **CRITICAL:** `POST /message` has zero auth — fix immediately
- CORS middleware (default same-origin)
- Default auth.enabled to true
- Hash API keys in SQLite (currently plaintext)
- Redact `_env_*`/`_header_*` from logs
- Session ownership (bind to agent_id + namespace)
- Namespace authorization (agent can only use assigned namespace)
- Bind to 127.0.0.1 in dev, 0.0.0.0 only in container

### Phase 5: Blocklist Gaps [P2] `mcproxy-s14`
- HTTP server validation (currently bypassed)
- Unify security.py and code_validator.py blocklists
- Admin refresh endpoint (POST /admin/blocklist/refresh)
- Hot reload on config change
- Complete server tier system (SAFE/NETWORK/SECRET/RISKY)
- Package extraction for pip/python -m patterns

### Phase 6: Dependency Cleanup [P3] `mcproxy-fe3`
- Remove `python-jose` (unmaintained, CVEs)
- Pin dependency versions
- Upper-bound `fastmcp`
- Fix ROADMAP.md duplicate v4.2 sections
- Fix stale Quadlet in /etc/containers/systemd/
- Move plaintext credentials out of mcproxy.json

## Key Files

| File | Role |
|------|------|
| `Dockerfile` | Container image build (multi-stage, shell removal, non-root) |
| `sandbox/executor.py` | Sandbox execution (1081 lines) — wraps user code |
| `sandbox/security.py` | Blocked imports/builtins constants |
| `code_validator.py` | AST-based dangerous pattern detection |
| `server/auth_middleware.py` | Auth middleware (static API keys) |
| `server/sse.py` | SSE/MCP endpoint handler |
| `server/__init__.py` | FastAPI app factory, route definitions |
| `blocklist.py` | Blocklist sync, validation, server classification |
| `auth/agent_registry.py` | Agent CRUD with SQLite backend |
| `auth/credential_store.py` | AES-256-GCM encrypted credential storage |

## Server2 Infrastructure

| Path | Purpose |
|------|---------|
| `/etc/systemd/system/mcproxy.service` | ACTIVE (bare service, to be replaced) |
| `/srv/containers/mcproxy/` | Config, venv, data, logs |
| `/srv/containers/mcproxy/mcproxy.json` | Main config (18 MCP servers, namespaces, auth) |
| `/srv/containers/mcproxy/.env` | API keys (PERPLEXITY_API_KEY, etc.) |
| `/srv/containers/mcproxy/Dockerfile` | Exists but image never built |
| `/srv/containers/mcproxy/mcproxy.container` | Hardened Quadlet (unused) |

## Audit Findings (for reference)

### Risk Matrix
| Vector | Risk | Key Finding |
|---|---|---|
| No container isolation | 🔴 CRITICAL | Bare systemd service on host |
| POST /message unauthenticated | 🔴 HIGH | Zero auth check on code execution endpoint |
| Sandbox: AST-only enforcement | 🔴 HIGH | Full `__builtins__`, `sys`/`ast` in scope |
| No CORS policy | 🟡 MEDIUM | No CORSMiddleware configured |
| API keys plaintext in SQLite | 🟡 MEDIUM | `api_key` column unhashed |
| Credentials in logs | 🟡 MEDIUM | `_env_*`/`_header_*` logged verbatim |
| Host networking in compose | 🟡 MEDIUM | `podman-compose.yml` has `network_mode: host` |
| Session ownership | 🟡 MEDIUM | No agent-to-session binding |
| `python-jose` unmaintained | 🟡 MEDIUM | Dead dependency with CVEs |
| HTTP servers bypass blocklist | 🟡 MEDIUM | Only npx/uvx validated |
| Input validation | 🟢 LOW | Proper parsing, parameterized SQL |
