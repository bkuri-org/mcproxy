"""Tool router - handles tools/call and routes to appropriate handler.

Supports two calling patterns:
1. Direct tool calls: server__tool_name → routed directly to upstream server
2. Dispatch meta-tool: dispatch (or mcproxy) → action-based handler (execute, search, etc.)
"""

import json
from typing import Any, Callable, Dict, Optional

from logging_config import get_logger
from manifest import CapabilityRegistry
from tool_aggregator import untransform_tool_name
from utils.param_normalize import normalize_params

from .execute import handle_execute, handle_trace
from .help import handle_help
from .inspect import handle_inspect
from .search import handle_search

logger = get_logger(__name__)

# Tool name separator for direct calls: server__tool
TOOL_SEPARATOR = "__"

# Canonical dispatch tool name (also accepts 'mcproxy' for backward compat)
DISPATCH_TOOL_NAMES = {"dispatch", "mcproxy"}


def _is_dispatch_tool(tool_name: str) -> bool:
    """Check if tool name is the dispatch meta-tool."""
    # Strip any pi-level prefix (e.g., mcproxy_dispatch → dispatch)
    canonical = tool_name.split("_", 1)[-1] if "_" in tool_name else tool_name
    return canonical in DISPATCH_TOOL_NAMES


def _parse_direct_tool(tool_name: str) -> Optional[tuple[str, str]]:
    """Parse a direct tool name into (server, tool) components.

    Args:
        tool_name: Tool name in format 'server__tool_name'

    Returns:
        Tuple of (server_name, tool_name) or None if not a direct tool
    """
    if TOOL_SEPARATOR not in tool_name:
        return None
    parts = tool_name.split(TOOL_SEPARATOR, 1)
    if len(parts) != 2:
        return None
    server, tool = parts
    if not server or not tool:
        return None
    return server, tool


async def _handle_direct_call(
    msg_id: Any,
    tool_name: str,
    arguments: Dict[str, Any],
    namespace: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
    tool_executor: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Handle a direct tool call (server__tool pattern).

    Validates namespace access and routes directly to upstream server.

    Args:
        msg_id: JSON-RPC message ID
        tool_name: Full prefixed tool name (server__tool)
        arguments: Tool call arguments
        namespace: Connection namespace/group for access control
        capability_registry: Capability registry for namespace validation
        tool_executor: Callable to execute tools on upstream servers

    Returns:
        MCP response with tool result or error
    """
    parsed = _parse_direct_tool(tool_name)
    if not parsed:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Invalid tool name format: {tool_name}",
            },
        }

    server_name, tool = parsed

    # Validate namespace access
    if namespace and capability_registry:
        allowed_servers, error = capability_registry.resolve_namespace_to_servers(
            namespace
        )
        if error or server_name not in allowed_servers:
            logger.warning(
                f"[DIRECT_CALL] Access denied: server '{server_name}' "
                f"not in namespace/group '{namespace}'"
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": f"Access denied: server '{server_name}' is not available "
                    f"in namespace/group '{namespace}'",
                },
            }

    # Execute directly on upstream server
    if not tool_executor:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": "Tool executor not initialized"},
        }

    try:
        # Untransform the tool name to recover the original upstream name
        upstream_tool = untransform_tool_name(server_name, tool)
        if upstream_tool != tool:
            logger.debug(
                f"[DIRECT_CALL] Tool name untransformed: {tool} -> {upstream_tool}"
            )

        # Normalize parameter names to match tool schema convention
        # (snake_case <-> camelCase)
        if capability_registry and capability_registry._manifest:
            tools_by_server = capability_registry._manifest.get(
                "tools_by_server", {}
            )
            server_tools = tools_by_server.get(server_name, [])
            for t in server_tools:
                if t.get("name") == upstream_tool:
                    input_schema = t.get("inputSchema", {})
                    if input_schema:
                        normalized = normalize_params(
                            server_name, upstream_tool, arguments, input_schema
                        )
                        if normalized is not arguments:
                            logger.info(
                                f"[PARAM_NORMALIZE] {server_name}__{upstream_tool}: "
                                f"parameters normalized to match schema"
                            )
                            arguments = normalized
                    break

        ns_context = f" namespace={namespace}" if namespace else ""
        logger.info(
            f"[DIRECT_CALL] {server_name}__{upstream_tool}{ns_context} args={list(arguments.keys())}"
        )

        result = await tool_executor(server_name, upstream_tool, arguments)

        # Normalize result to MCP content format
        content = _normalize_result(result)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except ValueError as e:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": str(e)},
        }
    except RuntimeError as e:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": str(e)},
        }
    except Exception as e:
        logger.error(f"[DIRECT_CALL_ERROR] {server_name}__{tool}: {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Tool execution failed: {e}"},
        }


def _normalize_result(result: Any) -> list:
    """Normalize upstream tool result to MCP content format.

    Args:
        result: Raw result from upstream server

    Returns:
        List of MCP content items
    """
    if isinstance(result, list):
        # Already in content format?
        if result and isinstance(result[0], dict) and "type" in result[0]:
            return result
        # Plain list → wrap as text
        return [{"type": "text", "text": json.dumps(result)}]

    if isinstance(result, dict):
        # Already has content key?
        if "content" in result:
            return result["content"]
        # Plain dict → wrap as text
        return [{"type": "text", "text": json.dumps(result)}]

    # String or other → wrap as text
    return [{"type": "text", "text": str(result)}]


async def handle_tools_call(
    msg_id: Any,
    params: Dict[str, Any],
    namespace: Optional[str] = None,
    session_id: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
    sandbox_executor: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    tool_executor: Optional[Callable] = None,
    mcproxy_config: Optional[Dict[str, Any]] = None,
    mcp_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handle tools/call request - route to appropriate handler.

    Supports:
    1. Direct tool calls: server__tool_name → upstream server
    2. Dispatch meta-tool: dispatch/mcproxy → action-based handler

    Args:
        msg_id: JSON-RPC message ID
        params: Tool call parameters
        namespace: Optional namespace context from X-Namespace header
        session_id: Optional session ID from X-Session-ID header
        capability_registry: Capability registry instance
        sandbox_executor: Sandbox executor instance
        session_manager: Session manager instance
        tool_executor: Callable to execute tools
        mcproxy_config: mcproxy.json configuration
        mcp_config: MCP client configuration

    Returns:
        MCP response with tool result or error
    """
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    ns_context = f" namespace={namespace}" if namespace else ""
    sess_context = f" session={session_id}" if session_id else ""
    logger.info(f"[TOOL_CALL] tool={tool_name}{ns_context}{sess_context}")

    try:
        # Route 1: Direct tool call (server__tool pattern)
        if TOOL_SEPARATOR in tool_name and not _is_dispatch_tool(tool_name):
            return await _handle_direct_call(
                msg_id,
                tool_name,
                arguments,
                namespace=namespace,
                capability_registry=capability_registry,
                tool_executor=tool_executor,
            )

        # Route 2: Dispatch meta-tool
        canonical = tool_name.replace("mcproxy_", "") if "_" in tool_name else tool_name
        if canonical not in DISPATCH_TOOL_NAMES:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}. "
                    f"Use 'dispatch' for meta-actions or 'server__tool' for direct calls.",
                },
            }

        action = arguments.get("action")
        if not action:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": "Missing required parameter: action",
                },
            }

        if action == "execute":
            return await handle_execute(
                msg_id,
                arguments,
                connection_namespace=namespace,
                session_id=session_id,
                sandbox_executor=sandbox_executor,
                session_manager=session_manager,
                tool_executor=tool_executor,
            )

        elif action == "search":
            merged_config = {**(mcp_config or {}), **(mcproxy_config or {})}
            search_config = merged_config.get("search", {})
            min_words = search_config.get("min_words", 2)
            max_tools = search_config.get("max_tools", 5)

            return await handle_search(
                msg_id,
                arguments,
                connection_namespace=namespace,
                capability_registry=capability_registry,
                min_words=min_words,
                max_tools=max_tools,
            )

        elif action == "inspect":
            return await handle_inspect(
                msg_id,
                arguments,
                connection_namespace=namespace,
                capability_registry=capability_registry,
            )

        elif action == "help":
            return handle_help(msg_id, arguments)

        elif action == "trace":
            return await handle_trace(
                msg_id,
                arguments,
                connection_namespace=namespace,
                session_id=session_id,
                sandbox_executor=sandbox_executor,
                session_manager=session_manager,
                tool_executor=tool_executor,
            )

        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": f"Unknown action: {action}. "
                    f"Supported actions: execute, search, inspect, help, trace",
                },
            }
    except Exception as e:
        logger.error(f"[TOOL_CALL_ERROR] {tool_name}: {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Tool execution failed: {e}"},
        }
