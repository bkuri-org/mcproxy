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
from typing import Any, Dict, List, Optional

import re

import requests

from logging_config import get_logger
from utils.fuzzy_match import suggest_best_match

logger = get_logger(__name__)


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

        self._reconnect_attempts = 0
        self._reconnect_backoff_until: float = 0.0
        self._last_health_check: Optional[float] = None
        self._last_error: Optional[str] = None
        self._health_task: Optional[asyncio.Task] = None

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return self._tools

    @tools.setter
    def tools(self, value: List[Dict[str, Any]]) -> None:
        self._tools = value

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
                    "clientInfo": {"name": "mcproxy", "version": "5.1.0"},
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

        timeout_seconds = self.tool_timeouts.get(tool_name, self.tool_timeout)

        logger.info(f"[CALL_TOOL_START] server={self.name} tool={tool_name}")

        try:
            response = self._send_request(
                method="tools/call",
                params={"name": tool_name, "arguments": arguments},
                id=f"call_{tool_name}",
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
                        id=f"call_{tool_name}",
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
                        id=f"call_{tool_name}",
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

        self._last_error = None
        logger.info(f"[CALL_TOOL_SUCCESS] tool={tool_name}")
        return response.get("result", {})

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
                    result = response.json()
                    if "result" in result or "error" in result:
                        return result
                except (json.JSONDecodeError, ValueError):
                    pass
                logger.warning(f"No valid JSON-RPC response from '{self.name}'")
                return None

            # SSE transport: scan for data: lines
            for line in response.iter_lines():
                if not line:
                    continue

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                if line_str.startswith("data:"):
                    data_str = line_str[5:].strip()
                    try:
                        result = json.loads(data_str)
                        if "result" in result:
                            return result
                        if "error" in result:
                            return result
                    except json.JSONDecodeError:
                        continue

            logger.warning(f"No valid JSON-RPC response from '{self.name}'")
            return None

        except requests.exceptions.Timeout:
            logger.error(f"Request to '{self.name}' timed out")
            raise RuntimeError(f"Request timed out: {method}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request to '{self.name}' failed: {e}")
            raise RuntimeError(f"Request failed: {e}")

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
        except RuntimeError as e:
            logger.warning(f"Health check error for '{self.name}': {e}")
            self._initialized = False
            self._last_error = str(e)
            if self.session:
                self.session.close()
                self.session = None

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
