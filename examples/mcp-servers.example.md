# MCP Server Classifications

## Classification Tiers

| Tier | Behaviour |
|------|-----------|
| **blocked** | Server is never started. Enforced via a fail-closed blocklist adapter. If the blocklist file is unreadable or contains errors, all servers are blocked. |
| **risky** | Server is started only when `acknowledged: true` is set in the server's config entry. |
| **unclassified** | Server is started with a one-time warning logged per server per process lifetime. |
| **secret** | Classification-only label. No implied guarantees about secret handling, redaction, or isolation. Secret-handling enforcement is out of scope. |

## Example Config Data

```json
{
  "mcpServers": {
    "safe-server": {
      "command": "node",
      "args": ["./safe-server/index.js"],
      "classification": "unclassified"
    },
    "risky-server": {
      "command": "node",
      "args": ["./risky-server/index.js"],
      "classification": "risky",
      "acknowledged": true
    },
    "blocked-server": {
      "command": "node",
      "args": ["./blocked-server/index.js"],
      "classification": "blocked"
    },
    "secret-server": {
      "command": "node",
      "args": ["./secret-server/index.js"],
      "classification": "secret"
    }
  }
}
```

## Notes

- The JSON config file must remain valid JSON — no comments are allowed.
- An invalid tier string causes a config error at startup or reload; the server is never degraded to unclassified.
- `enforce_server_classifications()` is called identically during server_manager.py startup and during config_watcher.py / config_reloader.py reload hot-paths. There is no reload bypass.
- The `secret` tier is a documentation and labeling convenience only. Assigning it does not trigger any additional enforcement logic. If you need secret-handling guarantees (e.g., credential redaction, isolated processes, audit logging), implement those outside the classification system.
