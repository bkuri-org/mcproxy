---
id: note-005
tags: [jesse_mcp_issues]
created: 2026-05-10T09:32:54.530891+00:00
source: docs/jesse_mcp_issues.md
---

# jesse_mcp_issues

# Jesse MCP Issues via MCProxy

Empirical observations from integrating `jesse-mcp` (Jesse trading bot MCP server) through `mcproxy` during the MaksiTrader leads pipeline project.

## Environment

| Component | Host | Address |
|-----------|------|---------|
| mcproxy | server2 (192.168.50.71) | port 12010 |
| jesse (REST API) | server2 | port 9100 |
| jesse-mcp | server2 | via mcproxy stdio |
| maxitrader | workstation | calls mcproxy over LAN |

## Retest Results (2026-03-29)

### Status: backtest() still broken via mcproxy

Despite the documented fixes (correct tool names, parameters, timeouts), `backtest()` still fails through mcproxy with "No response from server". Direct testing on server2 revealed the true root cause.

### Root Cause: Sync function blocks event loop, causing ClosedResourceError

The `backtest()` function in jesse-mcp is **synchronous** (`def backtest(...)`, not `async def`). It blocks the asyncio event loop for 4-30 seconds while polling the Jesse REST API. During this time:

1. The sync function blocks the event loop thread
2. mcproxy's `_read_message()` reads stdout with `readline()` — gets no data (backtest is still running)
3. mcproxy eventually gets EOF (empty readline) and returns `None`
4. mcproxy raises `RuntimeError: No response from server 'jesse'`
5. Meanwhile, when the backtest finally completes, the MCP SDK tries to call `session.respond()` to send the result
6. But the memory stream is already closed (client disconnected / mcproxy gave up)
7. `anyio.ClosedResourceError` is raised inside a TaskGroup
8. The TaskGroup error crashes the jesse-mcp process

**Full traceback captured with patched anyio TaskGroup:**

```
mcp/shared/session.py:134 → respond()
mcp/shared/session.py:349 → _send_response()
anyio/streams/memory.py:249 → send()
anyio/streams/memory.py:218 → send_nowait()
→ ClosedResourceError
```

### Direct Testing Confirms Backtest Works

Testing jesse-mcp directly on server2 (bypassing mcproxy) confirms the backtest itself succeeds:

```
$ printf '...initialize...notifications/initialized...tools/call backtest...' | \
  timeout 120 python -m jesse_mcp

INFO:jesse-mcp.rest-client:✅ Candle data validated for all routes
INFO:jesse-mcp.rest-client:📊 Estimated backtest time: 2304 candles, ~4.6s expected
INFO:jesse-mcp.rest-client:⏳ Backtest started, polling for completion...
INFO:jesse-mcp.rest-client:✅ Backtest finished in 4.1s
INFO:jesse-mcp.rest-client:✅ Retrieved backtest result: return=-26.27%, sharpe=-4.12
Error: unhandled errors in a TaskGroup (1 sub-exception)  ← response can't be sent
```

The backtest completes successfully in 4.1 seconds with real data, but the response is never sent to stdout because the memory stream is closed by the time the sync function returns.

### Verified Working Tools

| Tool | Status | Notes |
|------|--------|-------|
| `jesse_status()` | Works | Fast (< 30ms), no event loop blocking |
| `strategy_list()` | Works | Fast (< 10ms) |
| `get_exchanges()` | Works | Fast (< 3ms) |
| `cache_stats()` | Works | Fast (< 2ms) |
| `analyze_results(sanitized_dict)` | Works | Fast (< 2ms) |
| `monte_carlo(sanitized_dict)` | Works | No circular ref crash with clean dicts |
| `candles_import()` | 422 error | Jesse API bug (unrelated to mcproxy) |
| `backtest()` | **FAILS** | Sync function blocks event loop → ClosedResourceError |

### Additional Findings

1. **mcproxy sandbox caching**: Identical code patterns return cached results from previous executions (0.0s response with stale data). Workaround: `import time; time.sleep(2)` to force fresh process.

2. **Jesse backtest result caching**: jesse-mcp has built-in caching with 1-hour TTL. Second calls with different parameters may return first call's results. This is intentional.

3. **FastMCP upgrade (3.0.2 → 3.1.1)**: Did not fix the issue. The bug is in the sync function blocking the event loop, not in FastMCP itself.

4. **jesse-mcp `.venv` has broken symlinks**: `/srv/containers/jesse-mcp/.venv/bin/python` points to a non-existent Python 3.12 installation. mcproxy config uses the old venv at `/srv/containers/jesse-baremetal/venv/` which works.

## Fix Required

The `backtest()` function in jesse-mcp must be made **async** to avoid blocking the event loop:

```python
# Current (broken):
@mcp.tool
def backtest(...) -> Dict[str, Any]:
    result = client.backtest(...)  # blocks for 4-30 seconds
    return result

# Fixed:
@mcp.tool
async def backtest(...) -> Dict[str, Any]:
    result = client.backtest(...)  # still sync internally, but wrapped
    return result
```

Alternatively, the sync blocking call should be run in a thread:
```python
import asyncio
@mcp.tool
async def backtest(...) -> Dict[str, Any]:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, client.backtest, ...)
    return result
```

The same fix likely applies to `optimize()`, `walk_forward()`, and other long-running tools.

## Recommendations

1. **Fast tools via mcproxy.** Use mcproxy for status checks, strategy listing, and metadata queries. These complete in < 30ms and work reliably.

2. **Long-running tools bypass mcproxy.** Until jesse-mcp's sync functions are converted to async, call Jesse REST API directly for `backtest()`, `optimize()`, etc. The direct HTTP approach with polling is more reliable.

3. **The hybrid approach works well.** The architecture MaksiTrader adopted is effective:
   ```
   mcproxy → jesse_status(), strategy_list()       (discovery, < 30ms)
   direct HTTP → backtest(), session polling         (execution, 5-30s)
   mcproxy → monte_carlo() with sanitized data       (analysis, optional)
   ```

4. **Fix jesse-mcp's backtest() to be async.** This is the proper long-term fix. All tools that make blocking HTTP calls should use `asyncio.to_thread()` or `loop.run_in_executor()`.

## Previous Issue Reports (Archived)

<details>
<summary>Original Issue 1: "No response from server" (same root cause)</summary>

The "No response from server" error was originally attributed to timeouts and wrong tool names. While those were also issues at the time, the underlying problem has always been that the synchronous `backtest()` function blocks the event loop, causing the MCP SDK to fail when trying to send the response.
</details>

<details>
<summary>Original Issue 2: "Circular reference detected" (RESOLVED)</summary>

Fixed by sanitizing dicts before passing to MCP tools. Only primitive types (str, int, float, bool, list, dict) should be passed.
</details>

<details>
<summary>Original Issue 3: Empty Metrics (jesse-mcp side)</summary>

When a backtest completes with 0 trades, jesse-mcp returns "Error: Empty metrics". Handle "stopped" status explicitly in the polling loop.
</details>

<details>
<summary>Original Issue 4: Candle Import 422 (Jesse API Bug)</summary>

`candles_import()` returns HTTP 422 regardless of how it's called. Jesse API bug in deployed version. Non-blocking since candle data already exists.
</details>

## Cross-Reference

- **jesse-mcp source**: `/srv/containers/jesse-mcp/jesse_mcp/server.py` (line 191: `def backtest(...)`)
- **mcproxy server manager**: `/srv/containers/mcproxy/server_manager.py` (line 294: `_read_message()`)
- **mcproxy timeout config**: `server_manager.py` (`tool_timeout`, `tool_timeouts`)
- **mcproxy serialization**: `sandbox/runtime.py` (`_sanitize_for_json()`)

