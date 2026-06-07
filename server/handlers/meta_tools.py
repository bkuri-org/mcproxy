"""Meta-tool definitions and handlers for MCProxy."""

import json
from typing import Any, Callable, Dict, List, Optional

from manifest import CapabilityRegistry, ManifestQuery
from logging_config import get_logger

logger = get_logger(__name__)


# ============================================================================
# META_TOOLS Definition
# ============================================================================

META_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "mcproxy",
        "description": "Unified tool: execute (run code), search (find tools), inspect (get schemas), help (get docs). "
        "Response: {status, result, stdout, traceback}. "
        "Use api.server('name').tool(args) with servers from initialize instructions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["execute", "search", "inspect", "help"],
                    "description": "Action to perform: execute (run code), search (find tools), inspect (get tool schemas), help (get documentation)",
                },
                "code": {
                    "type": "string",
                    "description": "Code expression (for action='execute' or 'inspect'). Execute: api.server('name').tool(args). Inspect: api.server('name').tool or api.server('name')",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for tools (for action='search'). Returns descriptions only - use inspect for schemas.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace for context",
                },
                "timeout_secs": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (for action='execute')",
                },
                "retries": {
                    "type": "integer",
                    "description": "Number of retries for failed tool calls (for action='execute', default: 0)",
                    "default": 0,
                },

                "topic": {
                    "type": "string",
                    "description": "Help topic (for action='help', e.g., 'sandbox' for security restrictions)",
                },
                "trace": {
                    "type": "boolean",
                    "description": "Enable call tracing (for action='execute', default: false)",
                },
            },
            "required": ["action"],
        },
    },
]


# ============================================================================
# Helper Functions
# ============================================================================


# Import parse_inspect_code from the parsing module
from .parsing import parse_inspect_code


# ============================================================================
# Meta Tool Handlers
# ============================================================================


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
        mcproxy_config: mcproxy.json configuration
        mcp_config: MCP client configuration

    Returns:
        MCP response with tool result or error
    """
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    ns_context = f" namespace={namespace}" if namespace else ""
    sess_context = f" session={session_id}" if session_id else ""
    logger.info(f"[META_TOOL_CALL] tool={tool_name}{ns_context}{sess_context}")
    logger.info(
        f"[HTTP_HANDLER_ARGS] tool={tool_name} arguments={arguments} type={type(arguments)}"
    )

    try:
        # Support prefixed names (mcproxy_mcproxy, mcproxy)
        canonical_name = tool_name.replace("mcproxy_", "")

        if canonical_name != "mcproxy":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}. v3.1.0 only supports 'mcproxy' tool.",
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
                    "message": f"Unknown action: {action}. Supported actions: execute, search, inspect, help, trace",
                },
            }
    except Exception as e:
        logger.error(f"[META_TOOL_ERROR] {tool_name}: {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Tool execution failed: {e}"},
        }


def handle_help(msg_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Handle help action - return documentation about available actions.

    Args:
        msg_id: JSON-RPC message ID
        arguments: Help arguments (optional: topic)

    Returns:
        MCP response with help documentation
    """
    topic = arguments.get("topic", "").lower()

    if topic == "sandbox":
        help_data = {
            "topic": "sandbox",
            "description": "Security sandbox restrictions and blocked functions",
            "blocked_functions": [
                "eval()",
                "exec()",
                "compile()",
                "open() (file operations)",
                "input()",
                "__import__()",
                "breakpoint()",
                "hasattr()",
                "getattr()",
                "setattr()",
                "delattr()",
                "os.system()",
                "os.popen()",
                "subprocess.* (all subprocess calls)",
                "pickle.loads() / pickle.load()",
                "marshal.loads() / marshal.load()",
                "importlib.import_module()",
            ],
            "blocked_imports": [
                "os",
                "sys",
                "subprocess",
                "socket",
                "http",
                "urllib",
                "requests",
                "shutil",
                "tempfile",
                "multiprocessing",
                "pickle",
                "marshal",
                "importlib",
                "builtins",
            ],
            "how_to_check": {
                "blocked_functions": "get_blocked_functions()",
                "blocked_imports": "get_blocked_imports()",
                "blocked_attributes": "get_blocked_attributes()",
            },
            "examples": [
                {
                    "description": "Check blocked functions",
                    "code": "get_blocked_functions()",
                },
                {
                    "description": "Check blocked imports",
                    "code": "get_blocked_imports()",
                },
            ],
        }
    else:
        help_data = {
            "actions": {
                "execute": {
                    "description": "Run Python code with access to MCP tools",
                    "usage": "mcproxy(action='execute', code='...', namespace='...', timeout_secs=60, retries=0)",
                    "available_objects": {
                        "api": "Access MCP servers: api.server('name').tool(args)",
                        "parallel": "Execute multiple tool calls concurrently: parallel([lambda: ...])",
                        "mcproxy": "Call mcproxy from within code: mcproxy(action='search', ...)",
                    },
                    "parameters": {
                        "timeout_secs": "Optional execution timeout (default: 60 seconds)",
                        "retries": "Number of retries for failed tool calls (default: 0). Only retries on timeout/network errors with exponential backoff.",
                    },
                },
                "search": {
                    "description": "Discover available servers and tools (no schemas - use inspect for schemas)",
                    "usage": "mcproxy(action='search', query='...', namespace='...')",
                },
                "inspect": {
                    "description": "Get detailed schemas for a server's tools (parameters, types, required fields)",
                    "usage": "mcproxy(action='inspect', server='name', namespace='...')",
                },
                "help": {
                    "description": "Get help and documentation",
                    "usage": "mcproxy(action='help') or mcproxy(action='help', topic='sandbox')",
                    "available_topics": ["sandbox"],
                },
            },
            "examples": [
                {
                    "description": "Discover available servers",
                    "code": "mcproxy(action='search', query='', namespace='dev')",
                },
                {
                    "description": "Call a tool on a server",
                    "code": "mcproxy(action='execute', code='api.server(\"wikipedia\").search(query=\"Python\")', namespace='dev')",
                },
                {
                    "description": "Inspect tool schemas",
                    "code": "mcproxy(action='inspect', server='wikipedia', namespace='dev')",
                },
                {
                    "description": "Get sandbox restrictions",
                    "code": "mcproxy(action='help', topic='sandbox')",
                },
                {
                    "description": "Run parallel tool calls (faster than sequential)",
                    "code": 'mcproxy(action=\'execute\', code=\'results = parallel([\\n    lambda: api.server("wikipedia").search(query="Python"),\\n    lambda: api.server("wikipedia").search(query="JavaScript")\\n])\', namespace=\'dev\')',
                },
            ],
            "quick_start": {
                "step_1_discover": "mcproxy(action='search', query='', namespace='dev')",
                "step_2_inspect": "mcproxy(action='inspect', server='wikipedia', namespace='dev')",
                "step_3_execute": "mcproxy(action='execute', code='api.server(\"wikipedia\").search(query=\"Python\")', namespace='dev')",
            },
            "tips": {
                "parallel_execution": "Use parallel() for multiple tool calls - much faster than sequential",
                "timeout_errors": "If you see timeout errors, they're from upstream MCP servers (not mcproxy). Try increasing timeout_secs or use retries=3 for automatic retry.",
                "server_discovery": "Use search(query='') to list all available servers and their tool counts",
                "automatic_retries": "Use retries=N to automatically retry failed tool calls. Only retries on timeout/network errors with exponential backoff (100ms, 200ms, 400ms, ...).",
            },
        }

    content = [{"type": "text", "text": json.dumps(help_data)}]
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}


async def handle_search(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Dict[str, Any]:
    """Handle search meta-tool.

    Args:
        msg_id: JSON-RPC message ID
        params: Search parameters (query, namespace)
        connection_namespace: Namespace from connection context (X-Namespace header)
        capability_registry: Capability registry instance

    Returns:
        MCP response with search results
    """
    query = params.get("query", "")

    param_namespace = params.get("namespace")
    effective_namespace = param_namespace or connection_namespace

    log_ns = f" namespace={effective_namespace}" if effective_namespace else ""
    logger.debug(f"[SEARCH] query={query}{log_ns}")

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

        mq = ManifestQuery(capability_registry)
        results = mq.search(
            query,
            namespace=effective_namespace,
        )

        if not effective_namespace:
            results["warning"] = (
                "No namespace specified. Results include default servers only. "
                "Isolated namespaces (e.g., 'system', 'home') require explicit namespace parameter."
            )

        content = [{"type": "text", "text": json.dumps(results)}]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except Exception as e:
        logger.error(f"[SEARCH_ERROR] {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Search failed: {e}"},
        }


async def handle_execute(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    session_id: Optional[str] = None,
    sandbox_executor: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    tool_executor: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Handle execute meta-tool.

    Args:
        msg_id: JSON-RPC message ID
        params: Execution parameters (code, namespace, timeout_secs)
        connection_namespace: Namespace from connection context (X-Namespace header)
        session_id: Optional session ID for session-scoped storage
        sandbox_executor: Sandbox executor instance
        session_manager: Session manager instance
        tool_executor: Callable to execute tools

    Returns:
        MCP response with execution result
    """
    code = params.get("code")
    if not code:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": "Missing required parameter: code"},
        }

    param_namespace = params.get("namespace")
    effective_namespace = param_namespace or connection_namespace
    timeout_secs = params.get("timeout_secs")
    retries = params.get("retries", 0)

    log_ns = f" namespace={effective_namespace}" if effective_namespace else ""
    log_sess = f" session={session_id}" if session_id else ""
    logger.debug(f"[EXECUTE]{log_ns}{log_sess} timeout={timeout_secs}")

    try:
        if sandbox_executor is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": "Sandbox executor not initialized",
                },
            }

        if not effective_namespace:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": "Missing required parameter: namespace. "
                    "v2.0 requires explicit namespace for execute(). "
                    "Provide in params or via X-Namespace header.",
                },
            }

        session = None
        if session_manager is not None:
            session = await session_manager.get_or_create(session_id)

        result = await sandbox_executor.execute(
            code,
            namespace=effective_namespace,
            timeout_secs=timeout_secs,
            session=session,
            retries=retries,
        )

        tool_time_ms = result.get("tool_time_ms", 0)
        execution_time_ms = result.get("execution_time_ms", 0)
        overhead_ms = execution_time_ms - tool_time_ms
        result["overhead_ms"] = overhead_ms

        if tool_time_ms > 5000:
            logger.warning(
                f"[SLOW_TOOL]{log_ns}{log_sess} tool_time={tool_time_ms}ms "
                f"overhead={overhead_ms}ms - slowness is from upstream MCP server, not mcproxy"
            )

        content = [{"type": "text", "text": json.dumps(result)}]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except Exception as e:
        logger.error(f"[EXECUTE_ERROR] {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Execution failed: {e}"},
        }


async def handle_trace(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    session_id: Optional[str] = None,
    sandbox_executor: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    tool_executor: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Handle trace action - execute code with full call stack tracing.

    Args:
        msg_id: JSON-RPC message ID
        params: Execution parameters (code, namespace, timeout_secs)
        connection_namespace: Namespace from connection context
        session_id: Optional session ID
        sandbox_executor: Sandbox executor instance
        session_manager: Session manager instance
        tool_executor: Callable to execute tools

    Returns:
        MCP response with execution result and trace data
    """
    import time
    from typing import List, Dict as TDict, Any as TAny

    trace_events: List[TDict[str, TAny]] = []

    def add_event(
        step: str,
        data: Optional[TDict[str, TAny]] = None,
        duration_ms: Optional[int] = None,
    ):
        event = {
            "timestamp": time.time(),
            "step": step,
        }
        if data:
            event["data"] = data
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        trace_events.append(event)

    start_time = time.perf_counter()
    add_event("trace_start", {"action": "trace"})

    code = params.get("code")
    if not code:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": "Missing required parameter: code"},
        }

    param_namespace = params.get("namespace")
    effective_namespace = param_namespace or connection_namespace
    timeout_secs = params.get("timeout_secs")
    retries = params.get("retries", 0)

    add_event(
        "params_parsed",
        {
            "namespace": effective_namespace,
            "timeout_secs": timeout_secs,
            "retries": retries,
            "code_length": len(code),
        },
    )

    try:
        if sandbox_executor is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": "Sandbox executor not initialized",
                },
            }

        if not effective_namespace:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": "Missing required parameter: namespace",
                },
            }

        validate_start = time.perf_counter()
        is_valid, error = sandbox_executor.validate_code(code)
        validate_ms = int((time.perf_counter() - validate_start) * 1000)
        add_event(
            "code_validated",
            {
                "valid": is_valid,
                "error": error if error else None,
            },
            duration_ms=validate_ms,
        )

        if not is_valid:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": f"Validation error: {error}"},
            }

        session = None
        if session_manager is not None:
            session = await session_manager.get_or_create(session_id)
            add_event("session_created", {"session_id": session_id})

        exec_start = time.perf_counter()
        result = await sandbox_executor.execute(
            code,
            namespace=effective_namespace,
            timeout_secs=timeout_secs,
            session=session,
            retries=retries,
            trace=True,  # Enable tracing
        )
        exec_ms = int((time.perf_counter() - exec_start) * 1000)
        add_event(
            "sandbox_execution_complete",
            {
                "status": result.get("status"),
                "has_result": result.get("result") is not None,
                "has_traceback": result.get("traceback") is not None,
            },
            duration_ms=exec_ms,
        )

        total_ms = int((time.perf_counter() - start_time) * 1000)
        add_event(
            "trace_complete",
            {
                "total_duration_ms": total_ms,
                "event_count": len(trace_events),
            },
        )

        trace_result = {
            "execution_result": result,
            "trace": {
                "events": trace_events,
                "summary": {
                    "total_ms": total_ms,
                    "validation_ms": validate_ms,
                    "execution_ms": exec_ms,
                    "overhead_ms": total_ms - exec_ms,
                },
            },
        }

        content = [{"type": "text", "text": json.dumps(trace_result)}]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except Exception as e:
        logger.error(f"[TRACE_ERROR] {e}")
        add_event("error", {"error": str(e)})
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Trace failed: {e}"},
        }


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
        connection_namespace: Namespace from connection context
        capability_registry: Capability registry instance

    Returns:
        MCP response with tool schema or list of tool schemas
    """
    code = params.get("code")
    server_name, tool_name = parse_inspect_code(code or "")

    if not server_name:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32602,
                "message": "Missing code. Use code='api.server(\"name\").tool'",
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
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": f"Server '{server_name}' not found",
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
                    "message": f"Tool '{tool_name}' not found in server '{server_name}'",
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
