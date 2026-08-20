"""Lifecycle management for MCProxy v2.0 components."""

from typing import Any, Callable, Dict, List, Optional

from logging_config import get_logger
from manifest import CapabilityRegistry, EventHookManager
from sandbox import AccessControlConfig, SandboxExecutor
from sandbox.pool import SandboxPool

logger = get_logger(__name__)

server_manager: Optional[Any] = None
capability_registry: Optional[CapabilityRegistry] = None
event_hook_manager: Optional[EventHookManager] = None
sandbox_executor: Optional[SandboxExecutor] = None
session_manager: Optional[Any] = None
_tool_executor: Optional[Callable] = None
sandbox_pool: Optional[SandboxPool] = None


def set_server_manager(manager: Any) -> None:
    """Set the global server manager reference (for backward compatibility)."""
    global server_manager
    server_manager = manager


def init_v2_components(
    config: Optional[Dict] = None,
    tool_executor: Optional[Callable] = None,
    servers_tools: Optional[Dict[str, List]] = None,
    pool: Optional[SandboxPool] = None,
) -> None:
    """Initialize v2.0 components: CapabilityRegistry and SandboxExecutor.

    Args:
        config: Optional configuration dict for components
        tool_executor: Callable to execute tools (server_name, tool_name, args) -> result
        servers_tools: Dict mapping server name to list of tools (from server_manager.get_all_tools())
        pool: Optional SandboxPool for fast pooled execution
    """
    global \
        capability_registry, \
        sandbox_executor, \
        event_hook_manager, \
        _tool_executor, \
        sandbox_pool

    config = config or {}
    _tool_executor = tool_executor
    sandbox_pool = pool

    search_cache_ttl = config.get("search", {}).get("cache_ttl", 300)
    capability_registry = CapabilityRegistry(query_cache_ttl=search_cache_ttl)

    if config and "namespaces" in config:
        capability_registry._namespaces = config["namespaces"]

    if config and "groups" in config:
        capability_registry._groups = config["groups"]

    if servers_tools:
        logger.info(f"[V2_INIT] Building manifest from {len(servers_tools)} servers")
        capability_registry.build(servers_tools)
    else:
        logger.warning("[V2_INIT] No servers_tools provided, manifest will be empty")

    event_hook_manager = EventHookManager(capability_registry)

    if tool_executor and capability_registry:
        # Build servers dict with tools included
        servers_with_tools = {}
        tools_by_server = capability_registry._manifest.get("tools_by_server", {})
        for server_name, server_info in capability_registry._manifest.get(
            "servers", {}
        ).items():
            servers_with_tools[server_name] = {
                **server_info,
                "tools": tools_by_server.get(server_name, []),
            }

        sandbox_manifest = AccessControlConfig(
            servers=servers_with_tools,
            namespaces=capability_registry._namespaces,
            groups=capability_registry._groups,
        )
        sandbox_executor = SandboxExecutor(
            manifest=sandbox_manifest,
            tool_executor=tool_executor,
            uv_path=config.get("sandbox", {}).get("uv_path", "uv"),
            default_timeout_secs=config.get("sandbox", {}).get("timeout_secs", 60),
            max_concurrency=config.get("max_parallel", 5),
            pool=pool,
        )

    _log_manifest_stats()


async def init_sandbox_pool(
    tool_executor: Callable,
    config: Optional[Dict] = None,
) -> SandboxPool:
    """Initialize and start the sandbox pool.

    Args:
        tool_executor: Callable to execute tools
        config: Optional configuration dict

    Returns:
        Started SandboxPool instance
    """
    global sandbox_pool

    sandbox_config = config.get("sandbox", {}) if config else {}
    pool_config = sandbox_config.get("pool", {})

    sandbox_pool = SandboxPool(
        tool_executor=tool_executor,
        uv_path=sandbox_config.get("uv_path", "uv"),
        pool_size=pool_config.get("size", 3),
        max_pool_size=pool_config.get("max_size", 10),
        idle_timeout_secs=pool_config.get("idle_timeout_secs", 300.0),
        ipc_timeout_secs=float(sandbox_config.get("timeout_secs", 60)),
    )

    await sandbox_pool.start()
    logger.info(f"[POOL_INIT] Sandbox pool started: {sandbox_pool.stats()}")
    return sandbox_pool


async def shutdown_sandbox_pool() -> None:
    """Shutdown the sandbox pool."""
    global sandbox_pool

    if sandbox_pool:
        await sandbox_pool.stop()
        sandbox_pool = None
        logger.info("[POOL_SHUTDOWN] Sandbox pool stopped")


def refresh_manifest(servers_tools: Dict[str, List]) -> None:
    """Refresh the manifest when servers finish loading tools.

    Args:
        servers_tools: Dict mapping server name to list of tools
    """
    global capability_registry, sandbox_executor, _tool_executor

    if capability_registry is None:
        logger.warning("[REFRESH_MANIFEST] CapabilityRegistry not initialized")
        return

    logger.info(
        f"[REFRESH_MANIFEST] Rebuilding manifest from {len(servers_tools)} servers"
    )
    capability_registry.build(servers_tools)

    if sandbox_executor and capability_registry:
        # Build servers dict with tools included (same as init_v2_components)
        servers_with_tools = {}
        tools_by_server = capability_registry._manifest.get("tools_by_server", {})
        for server_name, server_info in capability_registry._manifest.get(
            "servers", {}
        ).items():
            servers_with_tools[server_name] = {
                **server_info,
                "tools": tools_by_server.get(server_name, []),
            }

        sandbox_executor._manifest = AccessControlConfig(
            servers=servers_with_tools,
            namespaces=capability_registry._namespaces,
            groups=capability_registry._groups,
        )

    _log_manifest_stats()


def _log_manifest_stats() -> None:
    """Log current manifest statistics."""
    tool_count = (
        capability_registry._manifest.get("tool_count", 0) if capability_registry else 0
    )
    server_count = (
        len(capability_registry._manifest.get("servers", {}))
        if capability_registry
        else 0
    )
    logger.info(f"[MANIFEST_STATS] {server_count} servers, {tool_count} tools")


def on_config_change(new_config: Dict) -> None:
    """Handle config change event - reload manifests and sandbox config.

    Args:
        new_config: New configuration dict
    """
    logger.info("[CONFIG_CHANGE] Reloading v2.0 components")
    if event_hook_manager:
        event_hook_manager.trigger("config_change", {"config": new_config})


def on_server_health(server_name: str, healthy: bool) -> None:
    """Handle server health change event.

    Args:
        server_name: Name of the affected server
        healthy: Whether the server is now healthy
    """
    status = "healthy" if healthy else "unhealthy"
    logger.info(f"[SERVER_HEALTH] {server_name} is now {status}")
    if event_hook_manager:
        event_hook_manager.trigger(
            "server_health", {"server": server_name, "healthy": healthy}
        )


def get_capability_registry() -> Optional[CapabilityRegistry]:
    """Get the global capability registry."""
    return capability_registry


def get_server_manager() -> Optional[Any]:
    """Get the global server manager."""
    return server_manager


def get_sandbox_executor() -> Optional[SandboxExecutor]:
    """Get the global sandbox executor."""
    return sandbox_executor


def get_session_manager() -> Optional[Any]:
    """Get the global session manager."""
    return session_manager


def get_tool_executor() -> Optional[Callable]:
    """Get the global tool executor."""
    return _tool_executor


def get_sandbox_pool() -> Optional[SandboxPool]:
    """Get the global sandbox pool."""
    return sandbox_pool
