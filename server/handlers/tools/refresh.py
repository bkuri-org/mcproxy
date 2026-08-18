"""Dispatch action: refresh — force an immediate tool-catalog refresh.

Re-fetches tools/list from upstream connectors and rebuilds the manifest in
one pass. Complements the 30s health-check auto-refresh for when an operator
wants it now, or suspects drift the name-based diff skips (forced refresh
rebuilds from raw upstream responses unconditionally).
"""

import json
from typing import Any, Dict

from logging_config import get_logger

logger = get_logger(__name__)


async def handle_refresh(msg_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Handle dispatch action='refresh'.

    Args:
        msg_id: JSON-RPC message id
        arguments: optional {"server": "name"} to refresh one server

    Returns:
        Per-server summary of refreshed tool counts.
    """
    # Deferred imports: server/__init__ imports the handler chain, so a
    # module-level `from server import ...` here would be circular.
    from server import get_server_manager, refresh_manifest

    wrapper = get_server_manager()
    if wrapper is None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": "Server manager not initialized"},
        }

    manager = getattr(wrapper, "manager", wrapper)  # unwrap HotReloadServerManager
    target = arguments.get("server")

    if target and target not in manager.servers:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32602,
                "message": f"Unknown server: {target}. Known: {sorted(manager.servers)}",
            },
        }

    connectors = [manager.servers[target]] if target else list(manager.servers.values())
    summary: Dict[str, Any] = {}

    for c in connectors:
        if not c.is_running():
            summary[c.name] = "down (health loop retries with backoff)"
            continue
        try:
            resp = c._send_request(method="tools/list", id="refresh")
        except Exception as e:
            summary[c.name] = f"error: {e}"
            continue
        tools = (resp or {}).get("result", {}).get("tools")
        if not tools:
            summary[c.name] = "no response (kept last known tools)"
            continue
        summary[c.name] = len(tools)
        c.tools = tools

    # Unconditional single rebuild — this is the "force".
    refresh_manifest(manager.get_all_tools())

    logger.info(f"[REFRESH] Forced catalog refresh: {json.dumps(summary)}")
    content = [{"type": "text", "text": json.dumps({"refreshed": summary})}]
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}
