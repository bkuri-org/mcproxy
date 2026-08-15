"""Tool router - handles tools/call and routes to appropriate handler.

Supports two calling patterns:
1. Direct tool calls: server__tool_name → routed directly to upstream server
2. Dispatch meta-tool: dispatch (or mcproxy) → action-based handler (execute, search, etc.)
"""

import json
from typing import Any, Callable, Dict, Optional

from logging_config import get_logger
from manifest import CapabilityRegistry
from manifest.example_gen import generate_tool_example
from tool_aggregator import untransform_tool_name
from utils.param_normalize import normalize_params
from utils.fuzzy_match import suggest_best_match

from .execute import handle_execute, handle_trace
from .help import handle_help
from .inspect import handle_inspect
from .schema_migration import apply_migration
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
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": str(e)}}
    except RuntimeError as e:
        # Enrich parameter errors with schema hints from capability_registry
        error_msg = str(e)
        error_data = _build_param_error_data(
            server_name, upstream_tool, error_msg, capability_registry
        )
        error_resp = {"code": -32000, "message": error_msg}
        if error_data is not None:
            error_resp["data"] = error_data
        return {"jsonrpc": "2.0", "id": msg_id, "error": error_resp}
    except Exception as e:
        logger.error(f"[DIRECT_CALL_ERROR] {server_name}__{tool}: {e}")
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": f"Tool execution failed: {e}"}}


# Patterns that indicate parameter validation errors
_PARAM_ERROR_PATTERNS = (
    "missing required parameter",
    "unknown parameter",
    "field required",
    "extra forbidden",
    "extra inputs not permitted",
    "unexpected keyword argument",
)


def _build_param_error_data(
    server_name: str,
    tool_name: str,
    error_msg: str,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Optional[Dict[str, Any]]:
    """Build enriched error data for parameter validation errors.

    Looks up the tool's inputSchema from the manifest and includes
    available/required parameters and fuzzy suggestions in the error
    response data field.

    Args:
        server_name: Server name
        tool_name: Tool name
        error_msg: The error message to analyze
        capability_registry: Capability registry with manifest data

    Returns:
        Dict with enrichment data, or None if not a param error
    """
    error_lower = error_msg.lower()
    if not any(pat in error_lower for pat in _PARAM_ERROR_PATTERNS):
        return None

    if not capability_registry or not capability_registry._manifest:
        return None

    tools_by_server = capability_registry._manifest.get("tools_by_server", {})
    server_tools = tools_by_server.get(server_name, [])

    tool_def = None
    for t in server_tools:
        if t.get("name") == tool_name:
            tool_def = t
            break

    if not tool_def:
        return None

    schema = tool_def.get("inputSchema")
    if not schema:
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    available_params = list(properties.keys())

    if not available_params:
        return None

    data: Dict[str, Any] = {
        "tool_name": f"{server_name}__{tool_name}",
        "available_parameters": available_params,
        "required_parameters": required,
        "inputSchema": schema,
        "description": tool_def.get("description", ""),
        "usage_example": generate_tool_example(
            server_name, tool_name, schema
        ),
    }

    # Extract bad param name and fuzzy-match
    bad_param = _extract_bad_param(error_msg)
    if bad_param:
        suggestion = suggest_best_match(
            bad_param, available_params, threshold=0.5, max_suggestions=3
        )
        if suggestion:
            data["suggestion"] = suggestion

    return data


def _extract_bad_param(error_msg: str) -> Optional[str]:
    """Extract a parameter name from an error message.

    Looks for patterns like "Unknown parameter 'filepath'" or
    "Missing required parameter 'path'".

    Args:
        error_msg: Error message to extract from

    Returns:
        Extracted parameter name, or None
    """
    import re

    # Match single-quoted param name
    match = re.search(r"parameter '([^']+)'", error_msg)
    if match:
        return match.group(1)
    # Match double-quoted param name
    match = re.search(r'parameter "([^"]+)"', error_msg)
    if match:
        return match.group(1)
    return None


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
        # ── Schema migration choke point ──────────────────────────────
        # All tool calls pass through here *before* any lookup or
        # routing.  apply_migration handles:
        #   • param mapping  (mapped-wins collision precedence)
        #   • chain fixpoint with cycle guard
        #   • deprecation registry + grace-period warnings
        # The schema_migrations dict is loaded from config in main.py
        # and threaded in via mcproxy_config.
        schema_migrations = (mcproxy_config or {}).get("schema_migrations", {})
        if schema_migrations:
            tool_name, arguments = apply_migration(
                tool_name, arguments, schema_migrations
            )
            logger.debug(
                f"[SCHEMA_MIGRATION] Rewritten → tool={tool_name} "
                f"args={list(arguments.keys())}"
            )

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

        # Route 2: Dispatch meta-tool (post-migration tool_name may
        # still resolve here if a migration rewrote the name)
        canonical = tool_name.replace("mcproxy_", "") if "_" in tool_name else tool_name
        if canonical not in DISPATCH_TOOL_NAMES:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}. "
                    f"Use 'dispatch' for meta-actions or 'server__tool' for direct calls.",
                    "data": {
                        "_note": "tool name may have been rewritten by "
                        "schema_migration; check schema_migrations config.",
                    },
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
                mcproxy_config=mcproxy_config,
            )

        elif action == "search":
            return await handle_search(
                msg_id,
                arguments,
                connection_namespace=namespace,
                capability_registry=capability_registry,
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
