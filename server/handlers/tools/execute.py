"""Meta-tool execute and trace handlers."""

import json
from typing import Any, Callable, Dict, List, Optional

from logging_config import get_logger

from server.handlers.tools.fallback import FallbackSelector, FailoverExhaustedError
from server.handlers.tools.mock import MockEngine
from server.handlers.tools.result_limiter import apply_result_limit
from server.cache.manager import CacheManager
from server.health import HealthTracker

logger = get_logger(__name__)

_health_tracker = HealthTracker()

_DEFAULT_MAX_RESULT_SIZE_BYTES = 50000


def _resolve_max_result_bytes(
    params: Dict,
    mcproxy_config: Optional[Dict],
) -> int:
    """Extract per-call max_result_size from params, clamp to config default.

    The key is removed from *params* so it is never forwarded to the MCP tool.
    If not provided the config-level ``cache.max_result_size_bytes`` is used
    (falling back to ``_DEFAULT_MAX_RESULT_SIZE_BYTES``).
    """
    per_call_max = params.pop("max_result_size", None)
    config_default = (
        (mcproxy_config or {}).get("cache", {}).get("max_result_size_bytes", _DEFAULT_MAX_RESULT_SIZE_BYTES)
    )
    if per_call_max is not None:
        return max(0, min(int(per_call_max), int(config_default)))
    return int(config_default)


async def handle_execute(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    session_id: Optional[str] = None,
    sandbox_executor: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    tool_executor: Optional[Callable] = None,
    mcproxy_config: Optional[Dict] = None,
    cache_manager: Optional[CacheManager] = None,
    mock_engine: Optional[MockEngine] = None,
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
        mcproxy_config: MCProxy configuration dict
        cache_manager: Optional cache manager for tool result caching
        mock_engine: Pre-configured MockEngine instance (explicit opt-in)

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

    # === Result size limiting (extract & clamp before any forwarding) ===
    max_result_bytes = _resolve_max_result_bytes(params, mcproxy_config)
    # === End result size limiting ===

    # === Fallback-aware tool executor routing ===
    fallback_selector: Optional[FallbackSelector] = None
    if tool_executor is not None and mcproxy_config is not None:
        try:
            fallback_selector = FallbackSelector(mcproxy_config)
            tool_executor = fallback_selector.wrap(tool_executor)
        except Exception as fb_err:
            logger.warning(
                f"[EXECUTE] FallbackSelector init failed, using raw executor: {fb_err}"
            )
    # === End fallback routing ===

    # === Mock engine (explicit opt-in only) ===
    request_mock = params.get("mock") if isinstance(params.get("mock"), dict) else {}
    mock_active = False

    if tool_executor is not None:
        # Request-level opt-in takes precedence, then pre-passed engine, then config
        if request_mock.get("enabled") is True:
            if mock_engine is None:
                mock_cfg = mcproxy_config.get("mock", {}) if mcproxy_config else {}
                mock_engine = MockEngine({**mock_cfg, **request_mock})
            else:
                mock_engine = mock_engine.with_overrides(request_mock)
            mock_active = True
            logger.info("[EXECUTE] MockEngine active (request-level opt-in)")
        elif mock_engine is not None:
            mock_active = True
            logger.info("[EXECUTE] MockEngine active (pre-configured, explicit opt-in)")
        elif mcproxy_config is not None and mcproxy_config.get("mock", {}).get("enabled", False) is True:
            try:
                mock_engine = MockEngine(mcproxy_config["mock"])
                mock_active = True
                logger.info("[EXECUTE] MockEngine active (config-level opt-in)")
            except Exception as mock_init_err:
                logger.warning(
                    f"[EXECUTE] MockEngine init from config failed, skipping: {mock_init_err}"
                )

        if mock_active and mock_engine is not None:
            tool_executor = mock_engine.wrap(tool_executor)
    # === End mock engine ===

    # === Thinking engine ===
    think_param = params.get("think")
    thinking_output: Optional[Dict[str, Any]] = None

    if tool_executor is not None and mcproxy_config is not None:
        from reasoning import ThinkEngine

        engine = ThinkEngine(mcproxy_config, tool_executor)

        if isinstance(think_param, str):
            # Specific engine requested by name
            thinking_output = await engine.think(code, engine_name=think_param)
        elif think_param is True or (think_param is None and mcproxy_config.get("reasoning", {}).get("auto_think", {}).get("enabled", True)):
            # Auto-think: analyze code and decide
            analysis = engine.analyze_code(code)
            if think_param is True or analysis.get("should_think"):
                if think_param is True:
                    logger.info(
                        f"[EXECUTE] think=True, using default engine '{engine.default_engine}'"
                    )
                else:
                    logger.info(
                        f"[EXECUTE] auto-think triggered: {analysis.get('reason')}"
                    )
                thinking_output = await engine.think(
                    code, analysis=analysis
                )
    # === End thinking engine ===

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

        session = None
        if session_manager is not None:
            session = await session_manager.get_or_create(session_id)

        result = await sandbox_executor.execute(
            code,
            namespace=effective_namespace or "",
            timeout_secs=timeout_secs,
            session=session,
            retries=retries,
            cache_manager=cache_manager,
        )

        # === Apply result size limit ===
        result = apply_result_limit(result, max_result_bytes)
        # === End result size limit ===

        tool_time_ms = result.get("tool_time_ms", 0)
        execution_time_ms = result.get("execution_time_ms", 0)
        overhead_ms = execution_time_ms - tool_time_ms
        result["overhead_ms"] = overhead_ms

        # Record health metrics (errors are redacted/truncated inside record())
        _health_tracker.record(
            tool_name=effective_namespace,
            success=result.get("status") != "error",
            latency_ms=tool_time_ms,
            caller=session_id or "anonymous",
            error=result.get("traceback"),
        )

        # Detect common agent syntax mistakes and provide corrective guidance
        if result.get("status") == "error" and result.get("traceback"):
            tb = result["traceback"]
            if "_ToolProxy.__call__()" in tb and "positional argument" in tb:
                result["traceback"] = (
                    "Tool call syntax error: positional arguments are not allowed. "
                    "Tool calls require KEYWORD arguments only.\n"
                    "CORRECT:   api.server('name').tool_name(param1='val1', param2='val2')\n"
                    "INCORRECT: api.server('name').tool_name({'param1': 'val1'})\n"
                    "INCORRECT: api.server('name').tool_name(param_dict)\n"
                    "If you have a dict, unpack it: api.server('name').tool_name(**my_dict)"
                )

        if tool_time_ms > 5000:
            logger.warning(
                f"[SLOW_TOOL]{log_ns}{log_sess} tool_time={tool_time_ms}ms "
                f"overhead={overhead_ms}ms - slowness is from upstream MCP server, not mcproxy"
            )

        # Annotate result when mock engine intercepted execution
        if mock_active and mock_engine is not None and mock_engine.has_intercepted():
            result["_mocked"] = True

        # Include thinking output if the engine ran
        if thinking_output is not None:
            result["thinking"] = thinking_output

        content = [{"type": "text", "text": json.dumps(result)}]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except FailoverExhaustedError as e:
        logger.error(f"[EXECUTE_ERROR] Failover exhausted (all endpoints unhealthy): {e}")
        _health_tracker.record(
            tool_name=effective_namespace,
            success=False,
            latency_ms=0,
            caller=session_id or "anonymous",
            error=str(e),
        )
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32001,
                "message": f"All tool endpoints unhealthy (failover exhausted): {e}",
            },
        }
    except Exception as e:
        logger.error(f"[EXECUTE_ERROR] {e}")
        _health_tracker.record(
            tool_name=effective_namespace,
            success=False,
            latency_ms=0,
            caller=session_id or "anonymous",
            error=str(e),
        )
        error_msg = str(e)
        # Detect common agent mistakes and provide corrective guidance
        if "_ToolProxy.__call__()" in error_msg and "positional argument" in error_msg:
            error_msg = (
                f"Tool call syntax error: {e}. "
                "Tool calls require KEYWORD arguments only. "
                "CORRECT: api.server('name').tool_name(param1='val1', param2='val2') "
                "INCORRECT: api.server('name').tool_name({'param1': 'val1'}) "
                "If you have a dict, unpack it: api.server('name').tool_name(**my_dict)"
            )
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Execution failed: {error_msg}"},
        }


async def handle_trace(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    session_id: Optional[str] = None,
    sandbox_executor: Optional[Any] = None,
    session_manager: Optional[Any] = None,
    tool_executor: Optional[Callable] = None,
    cache_manager: Optional[CacheManager] = None,
    mcproxy_config: Optional[Dict] = None,
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
        cache_manager: Optional cache manager for tool result caching
        mcproxy_config: MCProxy configuration dict

    Returns:
        MCP response with execution result and trace data
    """
    import time
    from typing import Dict as TDict, Any as TAny

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

    # === Result size limiting (extract & clamp before any forwarding) ===
    max_result_bytes = _resolve_max_result_bytes(params, mcproxy_config)
    # === End result size limiting ===

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
            cache_manager=cache_manager,
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

        # === Apply result size limit ===
        result = apply_result_limit(result, max_result_bytes)
        # === End result size limit ===

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
