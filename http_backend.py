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

import requests

from logging_config import get_logger

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
            error_msg = str(error_details)
            self._last_error = error_msg
            logger.error(
                f"[CALL_TOOL_REMOTE_ERROR] tool={tool_name} error={error_details}"
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
