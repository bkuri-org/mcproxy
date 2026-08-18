"""Main entry point for MCProxy.

Initializes logging, loads configuration, starts server manager,
and runs the FastAPI application with hot-reload support.
"""

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from config_reloader import ConfigReloader, HotReloadServerManager
from config_watcher import ConfigError, load_config
from logging_config import get_logger, setup_logging
from mcp_server import create_mcp_server
from server import (
    app,
    configure_auth,
    init_v2_components,
    refresh_manifest,
    set_server_manager,
)
from server.admin_routes import register_admin_routes
from server.lifecycle import init_sandbox_pool, shutdown_sandbox_pool
from server.handlers import set_mcproxy_config

logger = get_logger(__name__)

# Global references for graceful shutdown
config_reloader: Optional[ConfigReloader] = None
hot_reload_manager: Optional[HotReloadServerManager] = None


async def main() -> None:
    """Main application entry point."""
    global config_reloader, hot_reload_manager

    # Load environment variables from .env file
    env_file = Path(".env")
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    if key and not os.environ.get(key):
                        os.environ[key] = value

    parser = argparse.ArgumentParser(
        description="MCProxy - MCP Gateway Aggregator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --log                    # Run with stdout logging (HTTP mode)
  python main.py --stdio                  # Run as MCP server over stdio
  python main.py --port 8080              # Run on custom port
  python main.py --config servers.json    # Use custom config file
  python main.py --no-reload              # Disable hot-reload
        """,
    )

    parser.add_argument(
        "--log", action="store_true", help="Log to stdout (default: syslog)"
    )

    parser.add_argument(
        "--port", type=int, default=12010, help="Port to listen on (default: 12010)"
    )

    parser.add_argument(
        "--config",
        default="mcproxy.json",
        help="Path to configuration file (default: mcproxy.json)",
    )

    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )

    parser.add_argument(
        "--no-reload", action="store_true", help="Disable hot-reload of configuration"
    )

    parser.add_argument(
        "--reload-interval",
        type=float,
        default=1.0,
        help="Config file check interval in seconds (default: 1.0)",
    )

    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run as MCP server over stdio instead of HTTP (for direct MCP client integration)",
    )

    parser.add_argument(
        "--validate-connections",
        action="store_true",
        help="Validate all HTTP backend connections at startup and exit on failure",
    )

    args = parser.parse_args()

    # Setup logging - use stderr in stdio mode to avoid conflicts with JSON output on stdout
    setup_logging(use_stdout=args.log and not args.stdio, use_stderr=args.stdio)

    logger.info("=" * 50)
    logger.info("MCProxy Starting")
    logger.info("=" * 50)

    # Load configuration
    try:
        config = load_config(args.config)
        logger.info(
            f"Loaded configuration with {len(config.get('servers', []))} servers"
        )

        # Pass config to handlers for search settings
        set_mcproxy_config(config)
    except ConfigError as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    # Validate backend connections if requested
    if args.validate_connections:
        import requests

        http_servers = [
            s
            for s in config.get("servers", [])
            if s.get("enabled", True) and "url" in s
        ]
        if http_servers:
            logger.info(
                f"Validating {len(http_servers)} HTTP backend connections..."
            )
            failures = []
            for server in http_servers:
                name = server["name"]
                url = server["url"]
                timeout = server.get("timeout", 60)
                try:
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream, application/json",
                    }
                    headers.update(server.get("headers", {}))
                    payload = {
                        "jsonrpc": "2.0",
                        "id": "validate",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "mcproxy",
                                "version": "5.2.0",
                            },
                        },
                    }
                    resp = requests.post(
                        url, json=payload, headers=headers, timeout=timeout
                    )
                    resp.raise_for_status()
                    logger.info(f"  ✅ {name} ({url})")
                except Exception as e:
                    logger.error(f"  ❌ {name} ({url}): {e}")
                    failures.append(name)

            if failures:
                logger.error(
                    f"Connection validation failed for {len(failures)} "
                    f"server(s): {', '.join(failures)}"
                )
                sys.exit(1)
            else:
                logger.info("All backend connections validated ✅")

    # Initialize blocklist security system
    security_config = config.get("security", {})
    if security_config.get("blocklist_enabled", True):
        try:
            from blocklist import Blocklist

            blocklist = Blocklist(security_config)
            await blocklist.initialize()

            errors, warnings = blocklist.validate_servers(config.get("servers", []))
            if warnings:
                for warning in warnings:
                    logger.warning(f"[BLOCKLIST] {warning}")
            if errors:
                logger.error("[BLOCKLIST] Server validation failed:")
                for error in errors:
                    logger.error(f"  - {error}")
                sys.exit(1)

            logger.info("[BLOCKLIST] Server validation passed")
        except ImportError:
            logger.warning(
                "[BLOCKLIST] blocklist module not available, skipping validation"
            )
        except Exception as e:
            logger.warning(
                f"[BLOCKLIST] Initialization failed: {e}, skipping validation"
            )

    # Initialize authentication system
    auth_config = config.get("auth", {})
    if auth_config.get("enabled", False):
        from auth import (
            AgentRegistry,
            CredentialStore,
            CredentialError,
            ScopeResolver,
        )

        data_dir = Path("data")
        data_dir.mkdir(exist_ok=True)

        cred_db_path = auth_config.get("credentials_db", "data/credentials.db")
        agent_db_path = auth_config.get("agents_db", "data/agents.db")

        try:
            cred_store = CredentialStore(cred_db_path)
            logger.info(f"Initialized credential store at {cred_db_path}")
        except CredentialError as e:
            logger.error(f"Failed to initialize credential store: {e}")
            logger.error(
                "Set MCPROXY_CREDENTIAL_KEY environment variable (32-byte hex string)"
            )
            sys.exit(1)

        agent_registry = AgentRegistry(agent_db_path)
        logger.info(f"Initialized agent registry at {agent_db_path}")

        scope_mappings = auth_config.get("scope_mappings", {})
        tool_scopes = auth_config.get("tool_scopes", {})
        scope_resolver = ScopeResolver(cred_store, scope_mappings, tool_scopes)
        logger.info("Initialized scope resolver")

        from server.auth_middleware import configure_static_key_auth

        configure_static_key_auth(agent_registry, auth_config)
        logger.info("Authentication enabled (static API key)")

        admin_key_env = auth_config.get("admin_key_env", "MCPROXY_ADMIN_KEY")
        if not os.environ.get(admin_key_env):
            if auth_config.get("enabled", False):
                logger.warning(
                    "SECURITY: MCPROXY_ADMIN_KEY not set. Admin endpoints will only be "
                    "accessible from localhost. For production deployments, set "
                    f"{admin_key_env} environment variable to secure admin API."
                )

        if auth_config.get("enabled", False):
            register_admin_routes(app, agent_registry, auth_config)
            logger.info("Admin endpoints registered")
    else:
        logger.info("Authentication disabled")

    # Initialize hot-reload capable server manager
    def on_server_ready(server_name: str, tool_count: int) -> None:
        """Callback when a server finishes loading its tools."""
        logger.info(f"[SERVER_READY] {server_name} has {tool_count} tools")
        if hot_reload_manager:
            tools = hot_reload_manager.get_all_tools()
            refresh_manifest(tools)

    hot_reload_manager = HotReloadServerManager(config, on_server_ready=on_server_ready)
    set_server_manager(hot_reload_manager)

    # Spawn servers
    await hot_reload_manager.spawn_servers()

    # Give servers time to connect
    await asyncio.sleep(2)

    # Log status and warn about unreachable servers
    tools = hot_reload_manager.get_all_tools()
    total_tools = sum(len(t) for t in tools.values())
    connected_count = len(tools)
    expected_count = len(
        [s for s in config.get("servers", []) if s.get("enabled", True) and "url" in s]
    )
    logger.info(
        f"Servers ready: {connected_count}/{expected_count} connected with {total_tools} tools"
    )
    if connected_count < expected_count:
        logger.warning(
            f"{expected_count - connected_count} servers failed to connect - "
            f"check that adapter services are running"
        )

    # Initialize sandbox pool for fast execution
    pool = await init_sandbox_pool(
        tool_executor=hot_reload_manager.call_tool,
        config=config,
    )

    # Initialize v2.0 components (CapabilityRegistry, SandboxExecutor with pool)
    init_v2_components(
        config,
        tool_executor=hot_reload_manager.call_tool,
        servers_tools=tools,
        pool=pool,
    )
    logger.info("v2.0 components initialized with sandbox pool")

    # Link capability registry to hot_reload_manager for namespace/group updates
    from server import get_capability_registry

    cap_registry = get_capability_registry()
    if cap_registry and hasattr(hot_reload_manager, "set_capability_registry"):
        hot_reload_manager.set_capability_registry(cap_registry)
        logger.info("Linked capability registry for hot-reload updates")

    # Setup config reloader (hot-reload) - skip in stdio mode
    if not args.no_reload and not args.stdio:
        config_reloader = ConfigReloader(
            config_path=args.config,
            reload_callback=hot_reload_manager.reload_config,
            check_interval=args.reload_interval,
        )
        await config_reloader.start()
    else:
        if args.no_reload:
            logger.info("Hot-reload disabled")

    # Setup signal handlers for graceful shutdown
    def signal_handler(sig: int, frame) -> None:
        logger.info("Received shutdown signal")
        asyncio.create_task(shutdown())

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run in stdio mode or HTTP mode
    if args.stdio:
        logger.info("Starting MCProxy as MCP server over stdio")
        mcp_server = create_mcp_server(hot_reload_manager)
        try:
            logger.info("MCProxy MCP server running on stdio")
            # Use run_async to keep the same event loop and avoid thread/loop conflicts
            await mcp_server.run_async(transport="stdio", show_banner=False)
        except Exception as e:
            logger.error(f"MCP server error: {e}")
        finally:
            await shutdown()
    else:
        # Run uvicorn (HTTP/SSE mode)
        uvicorn_config = uvicorn.Config(
            app, host=args.host, port=args.port, log_level="warning"
        )
        server = uvicorn.Server(uvicorn_config)

        logger.info(f"Starting HTTP server on {args.host}:{args.port}")
        logger.info(f"SSE endpoint: http://{args.host}:{args.port}/sse")
        if not args.no_reload:
            logger.info(f"Hot-reload enabled (checking every {args.reload_interval}s)")

        try:
            await server.serve()
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            await shutdown()


async def shutdown() -> None:
    """Gracefully shutdown all servers and watchers."""
    logger.info("Shutting down...")

    # Stop config reloader
    if config_reloader:
        await config_reloader.stop()

    # Stop all servers
    if hot_reload_manager:
        await hot_reload_manager.stop_all()

    # Shutdown sandbox pool
    await shutdown_sandbox_pool()

    logger.info("Shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
