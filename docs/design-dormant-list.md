# Design: Minimal tool exposure with a dormant shelf

Owner-blessed 2026-10-19 (telegram: "pls proceed as planned; no rush on v2 but
tackle eventually"). Namespace/group design remains FROZEN (owner 2026-09-01) —
exposure is a NEW axis (per-namespace, per-session policy), not a regroup.

## Problem

`tools/list` ships every upstream tool with its full input schema. Schemas
dominate per-seat prompt tokens; most tools sit unused for most sessions.

## Policy

Per namespace, `tools/list` returns:

1. **Gateway tools** — `dispatch` meta-tool, always exposed (unchanged).
2. **Exposed hot-set** — tools matching the namespace's `exposed` entries
   (a bare `server` name exposes that server's tools; `server__tool` exposes
   one tool) **plus** tools promoted for this seat, with full schemas.
3. **Dormant shelf** — every other tool, one line each:
   `name :: gist(≤10 words) [health]`, delivered two ways:
   - a structured `result.dormant[]` array (`{name, server, gist, health}`);
   - a synthetic `dormant` **tool entry** whose description is the shelf text —
     v1 clients drop unknown result fields but always show tool descriptions,
     so this is what makes the shelf visible to models.

**Ordering is always (server name, tool name) — never recency** — so repeated
calls are byte-identical and upstream prompt caches stay valid. Gateway tools
stay first; the `dormant` entry sorts last.

## Promotion (describe/call → auto-pin, per seat)

- Direct call to a dormant tool: **blocked with a teach error** and promoted.
  Error contract: code `-32000`, message
  ``Tool '<server__tool>' is dormant — search/describe first. It has been
  promoted for this session; call it again to execute.``, plus
  `data: {status: "dormant", tool, next}`. The tool name is always included.
- `dispatch(action='inspect')` on a dormant tool (or whole server): promoted,
  inspect itself proceeds normally.
- Promotion state: per-seat LRU (`OrderedDict`), cap
  `promoted_cap_per_session` (default 15), oldest evicted. Seat = `X-Session-ID`
  (falls back to an `"anonymous"` bucket). **Seats are never shared** — this is
  the `cacheScope=per-seat` constraint (a promoted tool changes the list, so
  the list must never be cached across seats).
- Promoted tools appear with full schemas on the next `tools/list`.

## Caps

- Dormant shelf: `dormant_max_lines` lines (default 50). Overflow truncates the
  shelf and adds `result.dormant_overflow = {per_server: {srv: n}, hint}`
  where hint points at `dispatch(action='search')`.

## Health marks

Shelf lines carry `ok` / `degraded` / `unknown` from the shared
`server.health.HealthTracker` (`success_rate ≥ 0.9` → ok). Today nothing feeds
`record()`, so marks read `unknown` — honest until health recording is wired
(pre-existing gap, out of scope here).

## Config (mcproxy.json)

```json
"exposure": {
  "enabled": true,
  "promoted_cap_per_session": 15,
  "dormant_max_lines": 50,
  "namespaces": { "thinking": { "exposed": ["sequential_thinking", "think_tool"] } }
}
```

Default-off: no block, `enabled: false`, or namespace not listed ⇒ the tools/list
code path is byte-identical to before this change (no extra fields emitted).
Hot-reload does NOT pick this up (reloader only diffs `servers`), so exposure
config changes ride a container replace.

## Response extras (v2-shaped, harmless for v1 clients)

When exposure is active, `tools/list` results also carry `ttlMs: 60000` and
`cacheScope: "seat:<session-id|anonymous>"` — per-seat private, because
promotion makes the list vary per seat.

## pi-side finding (read, verified 261019)

pi does **not** re-fetch MCP `tools/list` per turn — tools are fetched at
server connect / registration change (`docs/extensions.md` §§ Tool exposure /
Activate tools dynamically / MCP servers), and per-turn activation is done
pi-side via `exposure: codemode|deferred` + `setActiveTools()`. Consequences:

- mcproxy's filtered list is authoritative at connect time; promotions reach
  the model **in-turn** through the teach error / inspect result, and the
  refreshed list lands on next connect. No mcproxy push mechanism in v1.
- The shelf must ride a tool entry (see above) or pi would never show it.
- Follow-up (NOT built, not trivial-adjacent): a tiny pi extension using
  `registerMcpServer`/`setActiveTools` mirroring promotions client-side; or
  v2 adoption (below) plus `notifications/tools/list_changed` from mcproxy.

## v2 mapping (2026-07-28 spec — design only, no code)

| v1 (this change) | v2 |
|---|---|
| `result.ttlMs` / `result.cacheScope` extras | first-class list fields, same semantics |
| synthetic `dormant` tool entry + `result.dormant[]` | `server/discover` + pageable listings; shelf text becomes discovery metadata |
| `X-Session-ID` seat key | per-seat cache scoping per spec's cacheScope rules |
| teach-error promotion | list_changed notification after promotion |

Adoption stays incremental: config fields already match v2 names.

## Verification gates (deterministic, code)

1. `pytest tests/test_exposure.py` green (21 tests: filter, order, LRU, caps,
   seat scoping, teach error, describe promotion, default-off byte-parity).
2. Full suite: same pass/fail set as pre-change baseline (repo has
   environment-level py3.14 failures unrelated to this change).
3. Live before/after on the `thinking` namespace (curl `/message`): tool count,
   dormant count, response bytes, schema bytes, ~tokens.
4. Two consecutive `tools/list` calls → identical `tools[]` ordering.
5. `inspect` of a dormant tool → appears exposed on next list.
6. Direct dormant call → teach error containing the tool name; second call
   executes.

## Rollback

Remove the `thinking` entry (or set `enabled: false`) and reload, or redeploy
the previous image. No exposure block ⇒ old behavior exactly.
