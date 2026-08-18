"""HTTP backend connector for mcproxy.

Allows mcproxy to connect to pre-existing MCP servers via HTTP/SSE
instead of spawning as child processes. This enables:
- Independent server lifecycle management via systemd
- Eliminates sandbox IPC issues
- Simpler architecture where servers run as standalone services
"""

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional

import re

import requests

from logging_config import get_logger
from server.result_limiter import apply_result_limit
from utils.fuzzy_match import suggest_best_match

try:
    from schema_migration import apply_migration
except ImportError:
    apply_migration = None  # graceful degradation when module not yet available

logger = get_logger(__name__)

_DEFAULT_MAX_RESULT_SIZE_BYTES = 50000


def _is_response_for(msg: Dict[str, Any], req_id: str) -> bool:
    """True if ``msg`` is the JSON-RPC response for request ``req_id``.

    Guards against the id-mismatch desync: an MCP server's SSE/HTTP stream can
    interleave notifications and even a stale or concurrent request's response
    ahead of the one we asked for. Without correlating on ``id`` the first
    result/error in the stream gets returned as our answer — silently wrong.

    - notifications / server-initiated requests carry a ``method`` → never a response
    - a response echoes the request ``id``; a mismatched id is skipped
    - a result/error with no ``id`` and no ``method`` is a legacy response
      (some non-conforming servers omit the id) → accepted as fallback
    """
    if "method" in msg:
        return False
    if "result" not in msg and "error" not in msg:
        return False
    msg_id = msg.get("id")
    return msg_id is None or msg_id == req_id


# Type mappings for human-friendly validation error messages
_TYPE_DISPLAY = {
    "string": "string (text)",
    "integer": "number",
    "number": "number",
    "boolean": "boolean (true/false)",
    "array": "list/array",
    "object": "object/dict",
}


# Pydantic error type -> human-friendly label
_PYDANTIC_TYPE_MAP = {
    "missing": "missing",
    "value_error.missing": "missing",
    "extra_forbidden": "extra",
    "extra": "extra",
}


def _format_upstream_error(tool_name: str, error_details: dict) -> str:
    """Format upstream JSON-RPC errors into human-friendly messages.

    Converts raw Pydantic validation errors into actionable messages
    with fuzzy-match suggestions for parameter names.

    Args:
        tool_name: Name of the tool that was called
        error_details: The 'error' dict from the upstream JSON-RPC response

    Returns:
        Human-readable error message
    """
    message = error_details.get("message", "")
    data = error_details.get("data")

    # Check if data is a list of Pydantic validation errors
    if isinstance(data, list):
        parts = []
        for err in data:
            if not isinstance(err, dict):
                continue
            parts.append(_format_pydantic_error(err))
        if parts:
            return "; ".join(parts)

    # Check if data is a dict with validation_error wrapper (some upstreams)
    if isinstance(data, dict):
        parsed = _try_parse_dict_error(data)
        if parsed:
            return parsed

    # Fallback: try parsing the message field for raw Pydantic multi-line format
    # e.g. "1 validation error for ModelName\n  param_name\n    error_msg (type=error_type)"
    parsed_message = _parse_pydantic_message(message)
    if parsed_message:
        return parsed_message

    # Non-Pydantic: use message as-is
    return message


def _format_pydantic_error(err: dict) -> str:
    """Format a single Pydantic validation error into a human-friendly message.

    Args:
        err: A Pydantic error dict with 'type', 'loc', 'msg' fields

    Returns:
        Human-readable error string
    """
    err_type = err.get("type", "")
    loc = err.get("loc", [])
    msg = err.get("msg", "")

    # Extract the parameter name from 'loc'
    # Pydantic loc is like ["body", "param_name"] or ["body", "nested", "param"]
    param = loc[-1] if loc and isinstance(loc[-1], str) else None

    # Missing required parameter
    if "missing" in err_type or "field required" in msg.lower():
        if param:
            return f"Missing required parameter '{param}'"
        return msg

    # Extra/unknown parameter
    if err_type in ("extra_forbidden", "extra") or "extra" in msg.lower():
        if param:
            return f"Unknown parameter '{param}'"
        return msg

    # Type mismatch
    if "json_type" in err_type or "type_error" in err_type or isinstance(msg, str) and "expected" in msg.lower():
        if param:
            input_type = err.get("input")
            input_display = type(input_type).__name__ if input_type is not None else "wrong type"
            return f"Parameter '{param}' has an invalid value"
        return msg

    # Fallback: use message as-is
    return msg if isinstance(msg, str) else str(msg)


def _detect_result_error(result: Dict[str, Any]) -> Optional[str]:
    """Detect validation errors hidden inside a successful result.

    Some upstream MCP servers (e.g., Home Assistant) return parameter
    validation errors as successful JSON-RPC results with error text
    in the content field instead of using the proper JSON-RPC error field.

    Detects:
    - Content text starting with "Error:" followed by JSON validation data
    - Content text that parses as a JSON array of Pydantic validation errors

    Args:
        result: The 'result' dict from a JSON-RPC tools/call response

    Returns:
        Human-readable error string, or None if the result is a genuine success
    """
    content = result.get("content", [])
    if not isinstance(content, list) or not content:
        return None

    # Check the first text content item for error patterns
    text = ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            break

    if not text:
        return None

    # Pattern 1: "Error:" prefix followed by JSON (Pydantic validation list)
    if text.startswith("Error:"):
        error_body = text[len("Error:"):].strip()
        # Try to parse as JSON — could be a Pydantic error list
        if error_body.startswith("["):
            try:
                parsed = json.loads(error_body)
                if isinstance(parsed, list):
                    parts = []
                    for err in parsed:
                        if isinstance(err, dict):
                            parts.append(_format_pydantic_error(err))
                    if parts:
                        return "; ".join(parts)
            except (json.JSONDecodeError, TypeError):
                pass
        # Non-JSON error body — return as-is with "Error:" stripped
        if error_body:
            return error_body
        return text

    # Pattern 2: Content that IS a JSON array of Pydantic errors (no prefix)
    # Must look like Pydantic errors: dicts with 'type', 'loc', or 'msg' fields.
    # Legitimate data arrays (e.g., list of kanban lists/cards) will not have
    # these fields and must not be misidentified as errors.
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list) and all(isinstance(e, dict) for e in parsed):
                # Verify at least one dict has Pydantic error markers
                is_pydantic = any(
                    "type" in e or "loc" in e or "msg" in e
                    for e in parsed
                )
                if is_pydantic:
                    parts = [_format_pydantic_error(e) for e in parsed]
                    if parts:
                        return "; ".join(parts)
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def _try_parse_dict_error(data: dict) -> Optional[str]:
    """Try to extract validation errors from a dict-format data field.

    Some upstream servers (e.g., Home Assistant MCP) return validation
    errors wrapped in a dict like:
        {"validation_error": [{"type": "missing", "loc": ["body", "x"], "msg": "field required"}]}

    Args:
        data: The 'data' dict from the JSON-RPC error response

    Returns:
        Formatted error string, or None if not parseable
    """
    # Look for common wrapper keys that contain a list of errors
    for key in ("validation_error", "errors", "detail"):
        value = data.get(key)
        if isinstance(value, list):
            parts = []
            for err in value:
                if isinstance(err, dict):
                    parts.append(_format_pydantic_error(err))
            if parts:
                return "; ".join(parts)
    return None


def _parse_pydantic_message(message: str) -> Optional[str]:
    """Parse raw Pydantic multi-line error format from the message field.

    Some upstream servers serialize Pydantic errors as a string in the
    message field instead of using the data field:
        "1 validation error for ToggleRequest\n  entity_id\n    field required (type=value_error.missing)"

    Args:
        message: The raw message string

    Returns:
        Human-readable error string, or None if not a Pydantic message
    """
    if not isinstance(message, str):
        return None

    # Match Pydantic multi-line format:
    # "N validation error(s) for ModelName\n  field_name\n    error_detail (type=error_type)"
    if not re.match(r"^\d+ validation error", message):
        return None

    lines = message.split("\n")
    parts = []
    # Skip header line, then pair field_name/detail lines.
    # Format: field_name (2-space indent) \n detail (4-space indent)
    i = 1  # skip header
    while i < len(lines) - 1:
        field_name = lines[i].strip()
        detail = lines[i + 1].strip()
        i += 2

        if not field_name or not detail:
            continue

        if field_name.startswith(("body", "query", "path")):
            field_name = field_name.split(".")[-1]

        # Extract Pydantic type from parentheses
        type_match = re.search(r"\(type=([^)]+)\)", detail)
        err_type = type_match.group(1) if type_match else ""

        if "missing" in err_type or "field required" in detail.lower():
            parts.append(f"Missing required parameter '{field_name}'")
        elif err_type in ("extra_forbidden", "extra") or "extra" in detail.lower():
            parts.append(f"Unknown parameter '{field_name}'")
        else:
            # Use the detail as-is
            parts.append(f"Parameter '{field_name}': {detail}")

    if parts:
        return "; ".join(parts)
    return None


class _ToolTimeoutError(RuntimeError):
    """Raised when a tool call to an upstream MCP server times out.

    Carries structured context so the router can return an informative
    error to the calling agent instead of a generic timeout message.
    """

    def __init__(
        self,
        server_name: str,
        method: str,
        timeout_secs: int,
        url: str,
    ):
        self.server_name = server_name
        self.method = method
        self.timeout_secs = timeout_secs
        self.url = url
        super().__init__(
            f"Upstream MCP server '{server_name}' timed out after {timeout_secs}s "
            f"on {method}. The server at {url} did not respond within the "
            f"configured timeout. This is NOT a mcproxy issue — the upstream server "
            f"is too slow to respond. Consider: (1) checking if the upstream server is "
            f"healthy, (2) increasing tool_timeout for this server in mcproxy.json, "
            f"or (3) fixing the upstream server code if it has an N+1 or performance bug."
        )


class _ConnectionError(RuntimeError):
    """Raised when a connection to an upstream MCP server fails.

    Carries structured context so the router can return an informative
    error to the calling agent.
    """

    def __init__(
        self,
        server_name: str,
        url: str,
        detail: str,
    ):
        self.server_name = server_name
        self.url = url
        self.detail = detail
        super().__init__(
            f"Connection to upstream MCP server '{server_name}' failed: {detail}. "
            f"The server at {url} is unreachable. This is NOT a mcproxy issue — "
            f"the upstream server is down or misconfigured."
        )


logger = get_logger(__name__)

LONG_RUNNING_TOOL_TIMEOUT_SECS = 350


class HTTPServerConnector:
    """Manages an MCP server connection via HTTP/SSE."""

    def __init__(
        self,
        name: str,
        url: str,
        timeout: int = 60,
        connect_timeout: int = 5,
        tool_timeout: Optional[int] = None,
        tool_timeouts: Optional[Dict[str, int]] = None,
        headers: Optional[Dict[str, str]] = None,
        on_tools_changed: Optional[Callable[[str, int], None]] = None,
    ):
        self.name = name
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.tool_timeout = tool_timeout or LONG_RUNNING_TOOL_TIMEOUT_SECS
        self.tool_timeouts = tool_timeouts or {}
        self.headers = headers or {
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
        }
        self.session: Optional[requests.Session] = None
        self.session_id: Optional[str] = None
        self._tools: List[Dict[str, Any]] = []
        self._initialized = False
        self._id_seq = 0

        self._reconnect_attempts = 0
        self._reconnect_backoff_until: float = 0.0
        self._last_health_check: Optional[float] = None
        self._last_error: Optional[str] = None
        self._health_task: Optional[asyncio.Task] = None
        self._on_tools_changed = on_tools_changed

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return self._tools

    @tools.setter
    def tools(self, value: List[Dict[str, Any]]) -> None:
        self._tools = value

    def _next_id(self, prefix: str = "req") -> str:
        """Monotonic per-connector request id so concurrent calls never collide."""
        self._id_seq += 1
        return f"{prefix}_{self._id_seq}"

    async def start(self) -> bool:
        try:
            logger.info(f"Connecting to HTTP server '{self.name}': {self.url}")

            self.session = requests.Session()
            self.session.headers.update(self.headers)

            init_response = self._send_request(
                method="initialize",
                params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcproxy", "version": "5.2.0"},
                },
                id="init",
            )

            if init_response is None or "error" in init_response:
                error_msg = str(init_response)
                self._last_error = f"Initialization failed: {error_msg}"
                logger.error(
                    f"Server '{self.name}' initialization failed: {init_response}"
                )
                return False

            logger.info(f"Initialized HTTP server '{self.name}'")
            self._initialized = True
            self._last_error = None
            self._reconnect_attempts = 0
            self._reconnect_backoff_until = 0.0

            await self._discover_tools()
            logger.info(
                f"HTTP server '{self.name}' connected with {len(self.tools)} tools"
            )
            return True

        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Failed to connect to HTTP server '{self.name}': {e}")
            return False

    async def stop(self) -> None:
        logger.info(f"Disconnecting from HTTP server '{self.name}'")
        await self.stop_health_check()
        if self.session:
            self.session.close()
        self.session = None
        self.session_id = None
        self._initialized = False

    def is_running(self) -> bool:
        return self._initialized and self.session is not None

    async def restart_if_needed(self) -> bool:
        if self.is_running():
            return True

        now = time.monotonic()
        if now < self._reconnect_backoff_until:
            remaining = self._reconnect_backoff_until - now
            logger.debug(
                f"HTTP server '{self.name}' reconnect backoff, {remaining:.1f}s remaining"
            )
            return False

        self._reconnect_attempts += 1
        backoff = min(2**self._reconnect_attempts, 60)

        logger.warning(
            f"HTTP server '{self.name}' reconnecting (attempt {self._reconnect_attempts}, "
            f"backoff {backoff}s)"
        )

        success = await self.start()
        if success:
            self._reconnect_attempts = 0
            self._reconnect_backoff_until = 0.0
        else:
            self._reconnect_backoff_until = time.monotonic() + backoff

        return success

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.is_running():
            raise RuntimeError(f"HTTP server '{self.name}' is not connected")

        # Extract per-call max_result_size from arguments before forwarding
        # to the upstream MCP tool. Clamp to config default.
        raw_max = arguments.pop("max_result_size", _DEFAULT_MAX_RESULT_SIZE_BYTES)
        try:
            max_result_size = int(raw_max)
        except (TypeError, ValueError):
            max_result_size = _DEFAULT_MAX_RESULT_SIZE_BYTES
        if max_result_size < 1 or max_result_size > _DEFAULT_MAX_RESULT_SIZE_BYTES:
            max_result_size = _DEFAULT_MAX_RESULT_SIZE_BYTES

        # Apply schema migrations so this connector always sends
        # canonical parameter names even when the caller (or router)
        # didn't rewrite them yet.
        if apply_migration is not None:
            try:
                arguments = apply_migration(arguments)
            except RuntimeError:
                # No schema_migrations configured — identity is correct.
                pass

        timeout_seconds = self.tool_timeouts.get(tool_name, self.tool_timeout)

        logger.info(f"[CALL_TOOL_START] server={self.name} tool={tool_name}")

        # Unique per-call id: two concurrent calls to the same tool must be
        # distinguishable, or the server's responses can't be told apart.
        req_id = self._next_id("call")

        try:
            response = self._send_request(
                method="tools/call",
                params={"name": tool_name, "arguments": arguments},
                id=req_id,
                timeout=timeout_seconds,
            )
        except RuntimeError as e:
            error_str = str(e)
            self._last_error = error_str
            if "404" in error_str or "session" in error_str.lower():
                logger.warning(f"Session expired for '{self.name}', reconnecting...")
                await self.stop()
                if await self.start():
                    response = self._send_request(
                        method="tools/call",
                        params={"name": tool_name, "arguments": arguments},
                        id=req_id,
                        timeout=timeout_seconds,
                    )
                else:
                    raise RuntimeError(f"Failed to reconnect to '{self.name}'")
            elif "401" in error_str or "Unauthorized" in error_str:
                logger.warning(
                    f"Authentication failure for '{self.name}', reconnecting to "
                    f"trigger re-auth..."
                )
                await self.stop()
                if await self.start():
                    response = self._send_request(
                        method="tools/call",
                        params={"name": tool_name, "arguments": arguments},
                        id=req_id,
                        timeout=timeout_seconds,
                    )
                else:
                    raise RuntimeError(
                        f"Failed to reconnect to '{self.name}' after 401"
                    )
            else:
                raise

        if response is None:
            raise RuntimeError(f"No response from HTTP server '{self.name}'")

        if "error" in response:
            error_details = response.get("error", {})
            error_msg = _format_upstream_error(tool_name, error_details)
            raw_msg = str(error_details)
            self._last_error = raw_msg
            logger.error(
                f"[CALL_TOOL_REMOTE_ERROR] tool={tool_name} error={raw_msg}"
            )
            raise RuntimeError(f"Tool call failed: {error_msg}")

        # Some upstream servers (e.g., Home Assistant MCP) return validation errors
        # as successful results with error content instead of JSON-RPC errors.
        # Detect these and convert to RuntimeErrors so downstream enrichment kicks in.
        result = response.get("result", {})
        result_error = _detect_result_error(result)
        if result_error:
            self._last_error = result_error
            logger.error(
                f"[CALL_TOOL_RESULT_ERROR] tool={tool_name} error={result_error[:200]}"
            )
            raise RuntimeError(f"Tool call failed: {result_error}")

        # Apply cumulative byte-budget limiter before returning the result.
        result = apply_result_limit(result, max_result_size)

        self._last_error = None
        logger.info(f"[CALL_TOOL_SUCCESS] tool={tool_name}")
        return result

    async def _discover_tools(self) -> None:
        response = self._send_request(method="tools/list", id="list_tools")

        if response and "result" in response and "tools" in response["result"]:
            self.tools = response["result"]["tools"]
            logger.debug(f"Discovered {len(self.tools)} tools from '{self.name}'")
        else:
            logger.warning(f"Failed to discover tools from '{self.name}': {response}")
            self.tools = []

    def _send_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        id: str = "1",
        timeout: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        if self.session is None:
            return None

        payload = {"jsonrpc": "2.0", "id": id, "method": method}
        if params:
            payload["params"] = params

        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        try:
            response = self.session.post(
                self.url,
                json=payload,
                headers=headers,
                stream=True,
                timeout=(self.connect_timeout, timeout or self.timeout),
            )

            new_session_id = response.headers.get("mcp-session-id")
            if new_session_id:
                self.session_id = new_session_id

            response.raise_for_status()

            content_type = response.headers.get("content-type", "")

            # Streamable HTTP (MCP 2025-06-18): plain JSON response
            if "application/json" in content_type:
                try:
                    msg = response.json()
                    if _is_response_for(msg, id):
                        return msg
                except (json.JSONDecodeError, ValueError):
                    pass
                logger.warning(
                    f"No JSON-RPC response matching id={id!r} from '{self.name}'"
                )
                return None

            # SSE transport: return the data: line that answers THIS request.
            # Skip notifications (they carry a "method") and any message whose
            # JSON-RPC id doesn't match — those belong to a different (stale or
            # concurrent) request and must not be returned as our answer.
            for line in response.iter_lines():
                if not line:
                    continue

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                if not line_str.startswith("data:"):
                    continue

                try:
                    msg = json.loads(line_str[5:].strip())
                except json.JSONDecodeError:
                    continue

                if _is_response_for(msg, id):
                    return msg
                logger.debug(
                    f"[{self.name}] skipping stream message "
                    f"(id={msg.get('id')!r}, method={msg.get('method')!r}) "
                    f"while waiting for id={id!r}"
                )

            logger.warning(f"No valid JSON-RPC response from '{self.name}'")
            return None

        except requests.exceptions.Timeout:
            effective_timeout = timeout or self.timeout
            logger.error(
                f"Request to '{self.name}' timed out after {effective_timeout}s "
                f"(method={method}, tool_timeout={self.tool_timeout}s, "
                f"default_timeout={self.timeout}s)"
            )
            raise _ToolTimeoutError(
                server_name=self.name,
                method=method,
                timeout_secs=effective_timeout,
                url=self.url,
            )
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection failed to '{self.name}': {e}")
            raise _ConnectionError(
                server_name=self.name,
                url=self.url,
                detail=str(e),
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Request to '{self.name}' failed: {e}")
            raise RuntimeError(
                f"Request to upstream server '{self.name}' failed: {e}"
            )

    def start_health_check(self, interval: int = 30) -> None:
        if self._health_task is not None and not self._health_task.done():
            logger.debug(f"Health check already running for '{self.name}'")
            return
        self._health_task = asyncio.create_task(self._health_check_loop(interval))
        logger.info(f"Started health check for '{self.name}' (interval={interval}s)")

    async def stop_health_check(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
            logger.debug(f"Stopped health check for '{self.name}'")

    async def _health_check_loop(self, interval: int) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await self._perform_health_check()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Health check loop error for '{self.name}': {e}")

    async def _perform_health_check(self) -> None:
        self._last_health_check = time.time()

        if not self.is_running():
            # Dead connector: self-heal via the existing reconnect backoff.
            if await self.restart_if_needed():
                self._notify_tools_changed()
            return

        try:
            response = self._send_request(method="tools/list", id="health")
            if response is None or "error" in response:
                logger.warning(
                    f"Health check failed for '{self.name}', marking disconnected"
                )
                self._initialized = False
                self._last_error = "Health check failed"
                if self.session:
                    self.session.close()
                    self.session = None
                return
            # Success: the health check IS a tools/list — capture it instead
            # of discarding it, so tool-cache staleness self-heals.
            self._maybe_update_tools(response)
        except RuntimeError as e:
            logger.warning(f"Health check error for '{self.name}': {e}")
            self._initialized = False
            self._last_error = str(e)
            if self.session:
                self.session.close()
                self.session = None

    def _maybe_update_tools(self, response: Dict[str, Any]) -> None:
        new_tools = (response.get("result") or {}).get("tools")
        if not new_tools:
            return  # empty list = upstream mid-restart; keep last known tools

        # ponytail: name-set comparison only; hash full schemas if schema
        # drift (same names, changed inputs) ever bites.
        old_names = sorted(t.get("name", "") for t in self._tools)
        new_names = sorted(t.get("name", "") for t in new_tools)
        if old_names == new_names:
            return

        logger.info(
            f"[TOOLS_CHANGED] '{self.name}' tool list changed "
            f"({len(old_names)} -> {len(new_names)}), refreshing manifest"
        )
        self.tools = new_tools
        self._notify_tools_changed()

    def _notify_tools_changed(self) -> None:
        if self._on_tools_changed:
            try:
                self._on_tools_changed(self.name, len(self._tools))
            except Exception as e:
                logger.error(f"tools-changed callback failed for '{self.name}': {e}")

    def update_config(
        self,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        tool_timeout: Optional[int] = None,
        tool_timeouts: Optional[Dict[str, int]] = None,
    ) -> None:
        url_changed = url is not None and url.rstrip("/") != self.url

        if url is not None:
            self.url = url.rstrip("/")
        if headers is not None:
            self.headers = headers
        if timeout is not None:
            self.timeout = timeout
        if tool_timeout is not None:
            self.tool_timeout = tool_timeout
        if tool_timeouts is not None:
            self.tool_timeouts = tool_timeouts

        if url_changed and self.is_running():
            logger.info(f"URL changed for '{self.name}', scheduling reconnect")
            self._initialized = False
            self._last_error = "URL changed, pending reconnect"

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "connected": self.is_running(),
            "tools_count": len(self._tools),
            "last_error": self._last_error,
            "reconnect_attempts": self._reconnect_attempts,
            "last_health_check": self._last_health_check,
        }
