"""Handler modules for MCProxy MCP server.

This package contains modular handlers for different aspects of MCP protocol handling:
- tools/ subpackage: Meta-tool definitions and handlers (mcproxy tool actions)
  - tools/__init__.py: META_TOOLS definition and exports
  - tools/router.py: handle_tools_call() - routes to appropriate handler
  - tools/execute.py: handle_execute(), handle_trace() - code execution
  - tools/search.py: handle_search() - tool discovery
  - tools/inspect.py: handle_inspect() - schema inspection
  - tools/help.py: handle_help() - documentation
- parsing.py: Parsing utilities for code expressions
- response.py: Response building utilities
"""

import json
from typing import Any, Callable, Dict, Optional

from fastapi import Request

from manifest import CapabilityRegistry, ManifestQuery
from manifest.typescript_gen import generate_compact_instructions
from sandbox import SandboxExecutor
from logging_config import get_logger
from session_stash import SessionManager, SessionStash

from server.sse import (
    get_namespace_from_request,
    get_session_id_from_request,
    validate_namespace,
)
from .tools import (
    META_TOOLS,
    handle_tools_call as meta_handle_tools_call,
    handle_help,
    handle_search,
    handle_execute,
    handle_trace,
    handle_inspect,
)
from .parsing import parse_inspect_code

logger = get_logger(__name__)


# ============================================================================
# Global Config Storage
# ============================================================================

_mcp_config: dict = {}
_mcproxy_config: dict = {}


def set_mcproxy_config(config: dict) -> None:
    """Set the mcproxy.json configuration.

    Args:
        config: Configuration dictionary from mcproxy.json
    """
    global _mcproxy_config
    _mcproxy_config = config


def get_mcproxy_config() -> dict:
    """Get the mcproxy.json configuration.

    Returns:
        Configuration dictionary from mcproxy.json
    """
    return _mcproxy_config


# ============================================================================
# Initialize Handler
# ============================================================================


async def handle_initialize(
    msg_id: Any,
    params: Dict[str, Any],
    namespace: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Dict[str, Any]:
    """Handle MCP initialize request.

    Args:
        msg_id: JSON-RPC message ID
        params: Initialize parameters (may contain 'config' from MCP client)
        namespace: Optional namespace context from X-Namespace header
        capability_registry: Capability registry instance

    Returns:
        MCP initialize response
    """
    # Store client config if provided
    global _mcp_config
    if isinstance(params, dict) and params.get("config"):
        _mcp_config = params.get("config", {})

    result = {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "mcproxy", "version": "5.1.0"},
    }

    # Generate TypeScript-style instructions from manifest
    if capability_registry and capability_registry._manifest:
        logger.debug(
            f"Manifest has {len(capability_registry._manifest.get('tools_by_server', {}))} servers with tools"
        )
        # Merge configs: mcproxy.json takes precedence over MCP client config
        merged_config = {**_mcp_config, **_mcproxy_config}
        instructions = generate_compact_instructions(
            capability_registry._manifest, config=merged_config
        )
        result["instructions"] = instructions
    else:
        logger.warning("No capability registry or manifest available during initialize")

    if namespace and capability_registry is not None:
        result["namespace"] = namespace
        servers, _ = capability_registry.resolve_namespace_to_servers(namespace)
        result["namespaceInfo"] = {
            "name": namespace,
            "servers": servers,
        }

    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


# ============================================================================
# Tools List Handler
# ============================================================================


async def handle_tools_list(
    msg_id: Any,
    namespace: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Dict[str, Any]:
    """Handle tools/list request - return dispatch meta-tool + per-server tools.

    Returns the dispatch meta-tool plus individual upstream tools prefixed
    with server name (e.g., perplexity_sonar__perplexity_search_web).
    Tools are filtered by the connection's namespace/group.

    Args:
        msg_id: JSON-RPC message ID
        namespace: Optional namespace context for filtering
        capability_registry: Capability registry for namespace resolution

    Returns:
        MCP response with tools list
    """
    tools = list(META_TOOLS)  # Start with dispatch meta-tool

    # Add per-server tools from manifest, filtered by namespace
    if capability_registry and capability_registry._manifest:
        allowed_servers = None
        if namespace:
            allowed_servers, _ = capability_registry.resolve_namespace_to_servers(
                namespace
            )

        tools_by_server = capability_registry._manifest.get("tools_by_server", {})
        for server_name, server_tools in tools_by_server.items():
            # Skip servers not in this namespace/group
            if allowed_servers is not None and server_name not in allowed_servers:
                continue

            for tool in server_tools:
                if not isinstance(tool, dict) or "name" not in tool:
                    continue

                prefixed_name = f"{server_name}__{tool['name']}"
                # Build a clean tool entry with the prefixed name
                tool_entry = {
                    "name": prefixed_name,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                }
                tools.append(tool_entry)

    logger.info(
        f"[TOOLS_LIST] Returning {len(tools)} tools "
        f"({len(META_TOOLS)} meta + {len(tools) - len(META_TOOLS)} direct)"
        f"{f' namespace={namespace}' if namespace else ''}"
    )

    return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}


# ============================================================================
# Tools Call Handler
# ============================================================================


async def handle_tools_call(
    msg_id: Any,
    params: Dict[str, Any],
    namespace: Optional[str] = None,
    session_id: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
    sandbox_executor: Optional[SandboxExecutor] = None,
    session_manager: Optional[SessionManager] = None,
    tool_executor: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Handle tools/call request - route to appropriate action handler.

    Args:
        msg_id: JSON-RPC message ID
        params: Tool call parameters
        namespace: Optional namespace context from X-Namespace header
        session_id: Optional session ID from X-Session-ID header
        capability_registry: Capability registry instance
        sandbox_executor: Sandbox executor instance
        session_manager: Session manager instance
        tool_executor: Callable to execute tools

    Returns:
        MCP response with tool result or error
    """
    return await meta_handle_tools_call(
        msg_id=msg_id,
        params=params,
        namespace=namespace,
        session_id=session_id,
        capability_registry=capability_registry,
        sandbox_executor=sandbox_executor,
        session_manager=session_manager,
        tool_executor=tool_executor,
        mcproxy_config=_mcproxy_config,
        mcp_config=_mcp_config,
    )


# ============================================================================
# Message Handler Factory
# ============================================================================


def create_message_handler(
    capability_registry_getter: Callable[[], Optional[CapabilityRegistry]],
    sandbox_executor_getter: Callable[[], Optional[SandboxExecutor]],
    session_manager_getter: Callable[[], Optional[SessionManager]],
    tool_executor_getter: Callable[[], Optional[Callable]],
) -> Callable:
    """Create a message handler with dependency injection.

    Args:
        capability_registry_getter: Callable that returns capability registry
        sandbox_executor_getter: Callable that returns sandbox executor
        session_manager_getter: Callable that returns session manager
        tool_executor_getter: Callable that returns tool executor

    Returns:
        Async message handler function
    """

    async def handle_message(
        request: Request, path_namespace: Optional[str] = None
    ) -> Dict[str, Any]:
        """Handle MCP messages from clients.

        Processes initialize, tools/list, and tools/call requests.
        v2.0 only supports search and execute meta-tools.
        Supports X-Namespace header for namespace context.
        Supports X-Session-ID header for session context.

        Args:
            request: FastAPI request object
            path_namespace: Namespace from URL path (takes precedence over header)
        """
        capability_registry = capability_registry_getter()
        sandbox_executor = sandbox_executor_getter()
        session_manager = session_manager_getter()
        tool_executor = tool_executor_getter()

        try:
            if not hasattr(request, "json"):
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32600,
                        "message": "Invalid request: expected FastAPI Request object",
                    },
                }
            body = await request.json()
            method = body.get("method")
            msg_id = body.get("id")
            params = body.get("params", {})

            header_ns = path_namespace or get_namespace_from_request(request)
            if header_ns and not validate_namespace(header_ns, capability_registry):
                logger.warning(f"[MESSAGE] Invalid X-Namespace header: {header_ns}")
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32602,
                        "message": f"Invalid namespace: {header_ns}",
                    },
                }

            session_id = get_session_id_from_request(request)

            ns_context = f" namespace={header_ns}" if header_ns else ""
            sess_context = f" session={session_id}" if session_id else ""
            logger.debug(f"[MESSAGE] method={method}{ns_context}{sess_context}")

            if method == "initialize":
                return await handle_initialize(
                    msg_id,
                    params,
                    namespace=header_ns,
                    capability_registry=capability_registry,
                )
            elif method == "tools/list":
                return await handle_tools_list(
                    msg_id,
                    namespace=header_ns,
                    capability_registry=capability_registry,
                )
            elif method == "tools/call":
                return await handle_tools_call(
                    msg_id,
                    params,
                    namespace=header_ns,
                    session_id=session_id,
                    capability_registry=capability_registry,
                    sandbox_executor=sandbox_executor,
                    session_manager=session_manager,
                    tool_executor=tool_executor,
                )
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

        except json.JSONDecodeError:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
            }
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}}

    return handle_message


# ============================================================================
# Response Building Utilities
# ============================================================================

from .response import (
    build_content_response,
    build_error_response,
    build_success_response,
    wrap_content,
)


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    # Meta-tools
    "META_TOOLS",
    "handle_tools_call",
    "handle_tools_list",
    "handle_help",
    "handle_search",
    "handle_execute",
    "handle_trace",
    "handle_inspect",
    "handle_initialize",
    # Parsing
    "parse_inspect_code",
    # Response building
    "build_success_response",
    "build_error_response",
    "build_content_response",
    "wrap_content",
    # Config
    "set_mcproxy_config",
    "get_mcproxy_config",
    # Message handler
    "create_message_handler",
]
