"""Server manager for connecting to HTTP/SSE MCP servers.

Handles connection lifecycle, tool discovery, and routing tool calls.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional

from sandbox import suggest_tool_fix
from logging_config import get_logger
from http_backend import HTTPServerConnector
from tool_aggregator import untransform_tool_name

logger = get_logger(__name__)


class ServerManager:
    """Manages multiple MCP server connections via HTTP/SSE."""

    def __init__(
        self,
        config: Dict[str, Any],
        on_server_ready: Optional[Callable[[str, int], None]] = None,
    ):
        self.config = config
        self.servers: Dict[str, HTTPServerConnector] = {}
        self._on_server_ready = on_server_ready

    async def spawn_servers(self) -> None:
        servers_config = self.config.get("servers", [])
        logger.info(
            f"Connecting to {len(servers_config)} servers with staggered startup..."
        )

        for i, server_config in enumerate(servers_config):
            if not server_config.get("enabled", True):
                logger.info(f"Skipping disabled server '{server_config['name']}'")
                continue

            if "command" in server_config and "url" not in server_config:
                logger.warning(
                    f"Skipping legacy stdio server '{server_config['name']}' - "
                    f"migrate to HTTP backend (add 'url' field)"
                )
                continue

            if "url" not in server_config:
                logger.warning(
                    f"Skipping server '{server_config['name']}' - missing 'url' field"
                )
                continue

            if i > 0:
                await asyncio.sleep(0.5)

            server = HTTPServerConnector(
                name=server_config["name"],
                url=server_config["url"],
                timeout=server_config.get("timeout", 60),
                connect_timeout=server_config.get("connect_timeout", 5),
                tool_timeout=server_config.get("tool_timeout"),
                tool_timeouts=server_config.get("tool_timeouts"),
                headers=server_config.get("headers"),
            )
            self.servers[server.name] = server
            asyncio.create_task(self._start_server(server))

    async def _start_server(self, server: HTTPServerConnector) -> None:
        try:
            success = await server.start()
            if success and self._on_server_ready:
                self._on_server_ready(server.name, len(server.tools))
            elif not success:
                logger.error(f"Server '{server.name}' failed to connect")
        except Exception as e:
            logger.error(f"Error connecting to server '{server.name}': {e}")

    async def stop_all(self) -> None:
        logger.info(f"Stopping {len(self.servers)} servers")
        await asyncio.gather(
            *[server.stop() for server in self.servers.values()],
            return_exceptions=True,
        )

    def get_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        tools: Dict[str, List[Dict[str, Any]]] = {}
        for name, server in self.servers.items():
            if server.is_running():
                tools[name] = server.tools
        return tools

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Any:
        if server_name not in self.servers:
            raise ValueError(f"Unknown server: {server_name}")

        server = self.servers[server_name]

        if not server.is_running():
            success = await server.restart_if_needed()
            if not success:
                raise RuntimeError(
                    f"Server '{server_name}' is not connected and failed to reconnect"
                )

        try:
            return await server.call_tool(tool_name, arguments)
        except RuntimeError as e:
            error_msg = str(e)
            if "not found" in error_msg.lower() or "unknown tool" in error_msg.lower():
                available_tools = [t.get("name", "") for t in server.tools]
                suggestion = suggest_tool_fix(tool_name, available_tools)
                if suggestion:
                    raise RuntimeError(
                        f"Tool '{tool_name}' not found on server "
                        f"'{server_name}'. {suggestion}"
                    ) from e
                # Include available tools from the server in the error
                tool_list = ", ".join(available_tools[:10])
                suffix = (
                    f" and {len(available_tools) - 10} more"
                    if len(available_tools) > 10
                    else ""
                )
                raise RuntimeError(
                    f"Tool '{tool_name}' not found on server '{server_name}'. "
                    f"Available tools: {tool_list}{suffix}"
                ) from e
            raise

    async def update_config(self, new_config: Dict[str, Any]) -> None:
        old_servers = {s["name"]: s for s in self.config.get("servers", [])}
        new_servers = {s["name"]: s for s in new_config.get("servers", [])}

        to_remove = set(old_servers.keys()) - set(new_servers.keys())
        to_add = set(new_servers.keys()) - set(old_servers.keys())
        to_check = set(old_servers.keys()) & set(new_servers.keys())

        to_update = []
        for name in to_check:
            old_url = old_servers[name].get("url", "")
            new_url = new_servers[name].get("url", "")
            if old_url != new_url:
                to_update.append(name)

        logger.info(
            f"Config update: +{len(to_add)} new, -{len(to_remove)} removed, "
            f"~{len(to_update)} updated"
        )

        for name in to_remove:
            if name in self.servers:
                await self.servers[name].stop()
                del self.servers[name]

        for name in to_update:
            if name in self.servers:
                await self.servers[name].stop()
                del self.servers[name]
            to_add.add(name)

        for name in to_add:
            server_config = new_servers[name]
            if not server_config.get("enabled", True):
                continue
            if "url" not in server_config:
                logger.warning(
                    f"Skipping server '{name}' during reload - missing 'url' field"
                )
                continue

            server = HTTPServerConnector(
                name=server_config["name"],
                url=server_config["url"],
                timeout=server_config.get("timeout", 60),
                tool_timeout=server_config.get("tool_timeout"),
                tool_timeouts=server_config.get("tool_timeouts"),
                headers=server_config.get("headers"),
            )
            self.servers[server.name] = server
            asyncio.create_task(self._start_server(server))

        self.config = new_config
