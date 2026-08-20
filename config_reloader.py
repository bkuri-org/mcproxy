"""Hot-reload configuration watcher for MCProxy.

Monitors mcp-servers.json for changes and reloads configuration
without dropping existing SSE connections.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from classification import enforce_server_classifications
from config_watcher import ConfigError, load_config
from logging_config import get_logger

logger = get_logger(__name__)


class ConfigReloader:
    """Watches configuration file and reloads on changes."""

    def __init__(
        self,
        config_path: str,
        reload_callback: Callable[[Dict[str, Any]], Any],
        check_interval: float = 1.0,
    ):
        """Initialize config reloader.

        Args:
            config_path: Path to mcp-servers.json
            reload_callback: Function or coroutine to call with new config on reload
            check_interval: Seconds between file checks (default: 1.0)
        """
        self.config_path = Path(config_path)
        self.reload_callback = reload_callback
        self.check_interval = check_interval
        self._last_mtime: Optional[float] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start watching for configuration changes."""
        if self._running:
            return

        self._running = True

        # Get initial mtime
        if self.config_path.exists():
            self._last_mtime = self.config_path.stat().st_mtime

        self._task = asyncio.create_task(self._watch_loop())
        logger.info(f"Started config watcher for {self.config_path}")

    async def stop(self) -> None:
        """Stop watching for configuration changes."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped config watcher")

    async def _watch_loop(self) -> None:
        """Main watch loop - polls file for changes."""
        while self._running:
            try:
                await self._check_for_changes()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in config watch loop: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_for_changes(self) -> None:
        """Check if config file has changed and reload if needed."""
        if not self.config_path.exists():
            return

        try:
            current_mtime = self.config_path.stat().st_mtime

            # Check if file has been modified
            if self._last_mtime is None or current_mtime != self._last_mtime:
                self._last_mtime = current_mtime

                # Small delay to ensure file is fully written
                await asyncio.sleep(0.1)

                await self._reload_config()
        except Exception as e:
            logger.error(f"Error checking config file: {e}")

    async def _reload_config(self) -> None:
        """Reload configuration and apply changes."""
        try:
            logger.info(f"Config change detected, reloading {self.config_path}")

            # Load and validate new config
            new_config = load_config(str(self.config_path))

            # Enforce server classifications on reload (identical path to startup)
            enforce_server_classifications(new_config)

            # Call the reload callback (handle both sync and async)
            result = self.reload_callback(new_config)
            if asyncio.iscoroutine(result):
                await result

            logger.info("Config reloaded successfully")

        except ConfigError as e:
            logger.error(f"Config validation failed, not reloading: {e}")
        except Exception as e:
            logger.error(f"Error reloading config: {e}")


class HotReloadServerManager:
    """Extension of ServerManager with hot-reload support."""

    def __init__(
        self,
        config: Dict[str, Any],
        on_server_ready: Optional[Callable[[str, int], None]] = None,
    ):
        """Initialize with config and prepare for hot reloads.

        Args:
            config: Configuration dictionary
            on_server_ready: Optional callback(server_name, tool_count) when server starts
        """
        from server_manager import ServerManager

        self.manager = ServerManager(config, on_server_ready=on_server_ready)
        self.current_config = config
        self._reloading = False
        self._capability_registry = None  # Will be set by set_capability_registry

    async def spawn_servers(self) -> None:
        """Delegate to underlying manager."""
        await self.manager.spawn_servers()

    async def stop_all(self) -> None:
        """Delegate to underlying manager."""
        await self.manager.stop_all()

    def get_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """Delegate to underlying manager."""
        return self.manager.get_all_tools()

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Any:
        """Delegate to underlying manager."""
        return await self.manager.call_tool(server_name, tool_name, arguments)

    def set_capability_registry(self, registry: Any) -> None:
        """Set the capability registry for hot-reload updates.

        Args:
            registry: CapabilityRegistry instance
        """
        self._capability_registry = registry
        logger.info("Capability registry linked for hot-reload")

    async def reload_config(self, new_config: Dict[str, Any]) -> None:
        """Hot-reload configuration without dropping connections.

        Strategy:
        1. Identify servers to add, remove, or update
        2. Stop removed servers
        3. Start new servers
        4. Update existing servers (if config changed)
        """
        if self._reloading:
            logger.warning("Already reloading, skipping")
            return

        self._reloading = True
        try:
            logger.info("Starting hot-reload...")

            old_servers = {s["name"]: s for s in self.current_config.get("servers", [])}
            new_servers = {s["name"]: s for s in new_config.get("servers", [])}

            # Find differences
            to_remove = set(old_servers.keys()) - set(new_servers.keys())
            to_add = set(new_servers.keys()) - set(old_servers.keys())
            to_check = set(old_servers.keys()) & set(new_servers.keys())

            # Check for config changes in existing servers
            to_update = []
            for name in to_check:
                if self._server_config_changed(old_servers[name], new_servers[name]):
                    to_update.append(name)

            logger.info(
                f"Hot-reload: +{len(to_add)} new, -{len(to_remove)} removed, "
                f"~{len(to_update)} updated"
            )

            # Stop removed servers
            for name in to_remove:
                if name in self.manager.servers:
                    logger.info(f"Stopping removed server '{name}'")
                    await self.manager.servers[name].stop()
                    del self.manager.servers[name]

            # Stop and restart updated servers
            for name in to_update:
                if name in self.manager.servers:
                    logger.info(f"Restarting updated server '{name}'")
                    await self.manager.servers[name].stop()
                    del self.manager.servers[name]
                to_add.add(name)  # Will be started as new

            # Start new/updated servers
            from http_backend import HTTPServerConnector

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
                    on_tools_changed=self.manager._on_server_ready,
                )
                self.manager.servers[server.name] = server
                asyncio.create_task(self.manager._start_server(server))

            # Update config reference
            self.current_config = new_config
            self.manager.config = new_config

            # Hot-reload namespaces and groups in capability registry
            if self._capability_registry:
                logger.info("Hot-reloading namespaces and groups...")
                # Update namespaces and groups directly from the new config
                self._capability_registry._namespaces = new_config.get("namespaces", {})
                self._capability_registry._groups = new_config.get("groups", {})
                logger.info(
                    f"Reloaded {len(self._capability_registry._namespaces)} namespaces, "
                    f"{len(self._capability_registry._groups)} groups"
                )

            logger.info("Hot-reload complete")

        finally:
            self._reloading = False

    def _server_config_changed(self, old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        fields = [
            "url",
            "headers",
            "timeout",
            "enabled",
            "tool_timeout",
            "tool_timeouts",
            "strip_tool_prefix",
            "tool_prefix",
        ]
        for field in fields:
            if old.get(field) != new.get(field):
                return True
        return False
