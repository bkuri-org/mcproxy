"""Meta-tool inspect handler."""

import json
from typing import Any, Dict, Optional

from manifest import CapabilityRegistry
from logging_config import get_logger
from server.handlers.parsing import parse_inspect_code
from server.health import HealthTracker

logger = get_logger(__name__)

_health_tracker = HealthTracker()


async def handle_inspect(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Dict[str, Any]:
    """Handle inspect action - get tool schema without executing.

    Args:
        msg_id: JSON-RPC message ID
        params: Inspection parameters:
            - code: Code expression like 'api.server("name")' or 'api.server("name").tool'
            - include_health: If true, include 24h rolling health metrics (requires inspect authz)
        connection_namespace: Namespace from connection context
        capability_registry: Capability registry instance

    Returns:
        MCP response with tool schema or list of tool schemas
    """
    code = params.get("code")
    server_name = params.get("server")
    tool_name = params.get("tool")
    include_health = params.get("include_health", False)

    if not server_name:
        server_name, tool_name = parse_inspect_code(code or "")

    if not server_name:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32602,
                "message": "Missing server name. Use server='name' or code='api.server(\"name\").tool'",
            },
        }

    try:
        if capability_registry is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": "Capability registry not initialized",
                },
            }

        # Get server tools
        manifest = capability_registry._manifest
        tools_by_server = manifest.get("tools_by_server", {})
        server_tools = tools_by_server.get(server_name)

        if not server_tools:
            available = sorted(tools_by_server.keys())[:15]
            available_str = ", ".join(available)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": (
                        f"Server '{server_name}' not found. "
                        f"Available servers: {available_str}. "
                        f"Use mcproxy(action='search') to discover servers."
                    ),
                },
            }

        # If tool name specified, return specific tool schema
        if tool_name:
            for tool in server_tools:
                if tool.get("name") == tool_name:
                    description = tool.get("description", "")
                    if not description or description.strip() == "":
                        words = tool_name.replace("_", " ").split()
                        tool_name_formatted = " ".join(
                            word.capitalize() for word in words
                        )
                        description = (
                            f"{tool_name_formatted} (description not provided)"
                        )
                        tool = dict(tool)
                        tool["description"] = description
                    if include_health:
                        health = _health_tracker.get_metrics(f"{server_name}:{tool_name}")
                        if health:
                            tool = dict(tool)
                            tool["health"] = health
                    content = [{"type": "text", "text": json.dumps(tool)}]
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"content": content},
                    }

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": (
                        f"Tool '{tool_name}' not found in server '{server_name}'. "
                        f"Available tools: {', '.join(t.get('name', '') for t in server_tools[:10])}. "
                        f"Use mcproxy(action='inspect', server='{server_name}') to see all tools."
                    ),
                },
            }

        # Otherwise return all tools for the server
        tools_info = []
        for tool in server_tools:
            description = tool.get("description", "")
            tool_name_local = tool.get("name", "")
            if not description or description.strip() == "":
                words = tool_name_local.replace("_", " ").split()
                tool_name_formatted = " ".join(word.capitalize() for word in words)
                description = f"{tool_name_formatted} (description not provided)"
            tool_info = {
                "name": tool_name_local,
                "description": description,
                "inputSchema": tool.get("inputSchema", {}),
            }
            if include_health:
                health = _health_tracker.get_metrics(f"{server_name}:{tool_name_local}")
                if health:
                    tool_info["health"] = health
            tools_info.append(tool_info)

        content = [{"type": "text", "text": json.dumps(tools_info)}]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except Exception as e:
        logger.error(f"[INSPECT_ERROR] {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Inspect failed: {e}"},
        }
