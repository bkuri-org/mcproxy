"""Integration tests for MCProxy v2.0 features."""

import json
import pytest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock, AsyncMock

from manifest import CapabilityRegistry, ManifestQuery, EventHookManager
from sandbox import (
    SandboxExecutor,
    AccessControlConfig,
    NamespaceAccessControl,
    ProxyAPI,
)
from config_watcher import load_config, validate_schema, ConfigError


class TestSearchExecuteFlow:
    """End-to-end tests for search → execute flow."""

    @pytest.fixture
    def integrated_system(self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]):
        registry = CapabilityRegistry()
        manifest = registry.build(sample_servers_tools)

        sandbox_manifest = AccessControlConfig(
            servers={
                name: {"tools": [t["name"] for t in tools]}
                for name, tools in sample_servers_tools.items()
            },
            namespaces={
                "browser": {"servers": ["playwright"], "extends": []},
                "files": {"servers": ["filesystem"], "extends": []},
                "crypto": {"servers": ["crypto"], "extends": []},
                "admin": {
                    "servers": ["playwright", "filesystem", "crypto", "system"],
                    "extends": [],
                },
            },
        )

        query = ManifestQuery(registry)

        tool_calls = []

        def tool_executor(server: str, tool: str, args: dict):
            tool_calls.append({"server": server, "tool": tool, "args": args})
            return {"status": "success", "server": server, "tool": tool}

        executor = SandboxExecutor(sandbox_manifest, tool_executor)

        return {
            "registry": registry,
            "query": query,
            "executor": executor,
            "tool_calls": tool_calls,
            "sandbox_manifest": sandbox_manifest,
        }

    def test_search_finds_tool(self, integrated_system: Dict[str, Any]):
        query = integrated_system["query"]
        results = query.search("navigate")

        assert results["total_matches"] >= 1
        assert any("navigate" in m for m in results["matches"]["tools"])

    def test_search_filters_by_namespace(self, integrated_system: Dict[str, Any]):
        query = integrated_system["query"]
        integrated_system["registry"].validate_inheritance(
            {
                "browser": {"servers": ["playwright"], "extends": []},
                "crypto": {"servers": ["crypto"], "extends": []},
            }
        )

        browser_results = query.search("navigate", namespace="browser")
        crypto_results = query.search("navigate", namespace="crypto")

        browser_servers = [r["server"] for r in browser_results["results"]]
        crypto_servers = [r["server"] for r in crypto_results["results"]]

        assert "playwright" in browser_servers
        assert "playwright" not in crypto_servers

    def test_validate_code_before_execution(self, integrated_system: Dict[str, Any]):
        executor = integrated_system["executor"]

        is_valid, _ = executor.validate_code("x = 1 + 2")
        assert is_valid

        is_valid, _ = executor.validate_code("import os")
        assert not is_valid

    def test_access_control_blocks_unauthorized_access(
        self, integrated_system: Dict[str, Any]
    ):
        sandbox_manifest = integrated_system["sandbox_manifest"]
        access_control = NamespaceAccessControl(sandbox_manifest)

        allowed, _ = access_control.can_access("browser", "playwright")
        assert allowed

        allowed, error = access_control.can_access("browser", "filesystem")
        assert not allowed
        assert "does not have access" in error

    def test_proxy_api_enforces_access_control(self, integrated_system: Dict[str, Any]):
        sandbox_manifest = integrated_system["sandbox_manifest"]
        access_control = NamespaceAccessControl(sandbox_manifest)
        api = ProxyAPI("browser", access_control, lambda *args: {"result": "ok"})

        proxy = api.server("playwright")
        assert proxy._server_name == "playwright"

        with pytest.raises(PermissionError):
            api.server("filesystem")


class TestNamespaceIsolation:
    """Tests for namespace-based isolation."""

    @pytest.fixture
    def isolated_namespaces(self) -> Dict[str, Any]:
        return {
            "crypto": {"servers": ["crypto"], "extends": []},
            "system": {"servers": ["system"], "extends": []},
            "browser": {"servers": ["playwright"], "extends": []},
            "admin": {
                "servers": ["system"],
                "extends": ["crypto", "browser"],
            },
        }

    @pytest.fixture
    def isolated_manifest(self, isolated_namespaces: Dict[str, Any]):
        return AccessControlConfig(
            servers={
                "crypto": {"tools": ["crypto__hash", "crypto__encrypt"]},
                "system": {"tools": ["system__execute", "system__reboot"]},
                "playwright": {"tools": ["playwright__navigate", "playwright__click"]},
            },
            namespaces=isolated_namespaces,
        )

    def test_crypto_cannot_access_system(self, isolated_manifest: AccessControlConfig):
        access_control = NamespaceAccessControl(isolated_manifest)

        allowed, _ = access_control.can_access("crypto", "crypto")
        assert allowed

        allowed, error = access_control.can_access("crypto", "system")
        assert not allowed
        assert "system" in error

    def test_browser_cannot_access_crypto(self, isolated_manifest: AccessControlConfig):
        access_control = NamespaceAccessControl(isolated_manifest)

        allowed, _ = access_control.can_access("browser", "playwright")
        assert allowed

        allowed, error = access_control.can_access("browser", "crypto")
        assert not allowed

    def test_admin_has_combined_access(self, isolated_manifest: AccessControlConfig):
        access_control = NamespaceAccessControl(isolated_manifest)

        allowed, _ = access_control.can_access("admin", "crypto")
        assert allowed

        allowed, _ = access_control.can_access("admin", "system")
        assert allowed

        allowed, _ = access_control.can_access("admin", "playwright")
        assert allowed

    def test_get_allowed_tools_respects_isolation(
        self, isolated_manifest: AccessControlConfig
    ):
        access_control = NamespaceAccessControl(isolated_manifest)

        crypto_tools, _ = access_control.get_allowed_tools("crypto", "crypto")
        assert "crypto__hash" in crypto_tools
        assert len(crypto_tools) == 2

        system_tools, error = access_control.get_allowed_tools("crypto", "system")
        assert system_tools == []
        assert "does not have access" in error

    def test_proxy_api_isolated_view(self, isolated_manifest: AccessControlConfig):
        access_control = NamespaceAccessControl(isolated_manifest)

        crypto_api = ProxyAPI("crypto", access_control, lambda *args: None)
        crypto_manifest = crypto_api.manifest()

        assert "crypto" in crypto_manifest["allowed_servers"]
        assert "system" not in crypto_manifest["allowed_servers"]

        admin_api = ProxyAPI("admin", access_control, lambda *args: None)
        admin_manifest = admin_api.manifest()

        assert "crypto" in admin_manifest["allowed_servers"]
        assert "system" in admin_manifest["allowed_servers"]
        assert "playwright" in admin_manifest["allowed_servers"]


class TestManifestRefreshOnConfigChange:
    """Tests for manifest refresh when config changes."""

    @pytest.fixture
    def refresh_system(self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]):
        registry = CapabilityRegistry()
        manifest = registry.build(sample_servers_tools)
        manager = EventHookManager(registry)

        return {"registry": registry, "manager": manager, "manifest": manifest}

    def test_config_change_invalidates_cache(self, refresh_system: Dict[str, Any]):
        registry = refresh_system["registry"]
        manager = refresh_system["manager"]

        assert registry._manifest != {}

        manager.trigger("config_change")

        assert registry._manifest == {}

    def test_server_health_updates_status(
        self,
        refresh_system: Dict[str, Any],
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
    ):
        registry = refresh_system["registry"]
        manager = refresh_system["manager"]

        manager.trigger("server_health", {"server": "playwright", "status": "degraded"})

        assert registry._manifest["servers"]["playwright"]["status"] == "degraded"

    def test_manual_trigger_invalidates(self, refresh_system: Dict[str, Any]):
        registry = refresh_system["registry"]
        manager = refresh_system["manager"]

        manager.trigger("manual")

        assert registry._manifest == {}

    def test_hooks_execute_on_config_change(self, refresh_system: Dict[str, Any]):
        manager = refresh_system["manager"]

        call_count = [0]

        def hook():
            call_count[0] += 1

        manager.register_hook("config_change", hook)
        manager.trigger("config_change")

        assert call_count[0] == 1

    def test_rebuild_after_invalidation(
        self,
        refresh_system: Dict[str, Any],
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
    ):
        registry = refresh_system["registry"]
        manager = refresh_system["manager"]

        manager.trigger("config_change")

        assert registry._manifest == {}

        new_manifest = registry.build(sample_servers_tools)
        assert new_manifest["tool_count"] == 8

    def test_event_history_tracks_changes(self, refresh_system: Dict[str, Any]):
        manager = refresh_system["manager"]

        manager.trigger("config_change")
        manager.trigger("server_health", {"server": "test", "status": "ok"})

        history = manager.get_event_history()
        assert len(history) == 2
        assert history[0]["event_type"] == "config_change"
        assert history[1]["event_type"] == "server_health"


class TestEndToEndWorkflow:
    """Complete workflow integration tests."""

    @pytest.fixture
    def full_system(self, tmp_path: Path):
        config = {
            "servers": [
                {
                    "name": "playwright",
                    "command": "npx",
                    "args": ["-y", "@playwright/mcp"],
                },
                {"name": "filesystem", "command": "npx", "args": ["-y", "@fs/mcp"]},
                {"name": "crypto", "command": "npx", "args": ["-y", "@crypto/mcp"]},
            ],
            "namespaces": {
                "browser": {"servers": ["playwright"], "extends": []},
                "files": {"servers": ["filesystem"], "extends": []},
                "secure": {"servers": ["crypto"], "extends": []},
                "full": {
                    "servers": ["playwright"],
                    "extends": ["files", "secure"],
                },
            },
            "manifests": {"startup_dwell_secs": 1.0},
            "sandbox": {"timeout_secs": 30},
        }

        config_file = tmp_path / "mcp-servers.json"
        config_file.write_text(json.dumps(config))

        servers_tools = {
            "playwright": [
                {"name": "playwright__navigate", "description": "Navigate to URL"},
                {"name": "playwright__click", "description": "Click element"},
            ],
            "filesystem": [
                {"name": "filesystem__read", "description": "Read file"},
                {"name": "filesystem__write", "description": "Write file"},
            ],
            "crypto": [
                {"name": "crypto__hash", "description": "Hash data"},
            ],
        }

        registry = CapabilityRegistry()
        registry.build(servers_tools)
        registry.validate_inheritance(config["namespaces"])

        sandbox_manifest = AccessControlConfig(
            servers={
                name: {"tools": [t["name"] for t in tools]}
                for name, tools in servers_tools.items()
            },
            namespaces=config["namespaces"],
        )

        return {
            "config": config,
            "config_file": config_file,
            "registry": registry,
            "sandbox_manifest": sandbox_manifest,
            "servers_tools": servers_tools,
        }

    def test_config_loads_successfully(self, full_system: Dict[str, Any]):
        config = load_config(str(full_system["config_file"]))
        assert len(config["servers"]) == 3
        assert "namespaces" in config

    def test_manifest_builds_from_tools(self, full_system: Dict[str, Any]):
        registry = full_system["registry"]
        assert registry._manifest["tool_count"] == 5

    def test_namespace_resolution_works(self, full_system: Dict[str, Any]):
        registry = full_system["registry"]

        full_servers = registry.resolve_namespace("full")
        assert set(full_servers) == {"playwright", "filesystem", "crypto"}

    def test_access_control_enforces_namespaces(self, full_system: Dict[str, Any]):
        access_control = NamespaceAccessControl(full_system["sandbox_manifest"])

        allowed, _ = access_control.can_access("browser", "playwright")
        assert allowed

        allowed, _ = access_control.can_access("full", "filesystem")
        assert allowed

        allowed, _ = access_control.can_access("browser", "crypto")
        assert not allowed

    def test_search_with_namespace_filter(self, full_system: Dict[str, Any]):
        query = ManifestQuery(full_system["registry"])

        results = query.search("navigate", namespace="browser")
        browser_servers = [r["server"] for r in results["results"]]
        assert "playwright" in browser_servers

        results = query.search("hash", namespace="browser")
        browser_servers = [r["server"] for r in results["results"]]
        assert "crypto" not in browser_servers

    def test_sandbox_code_validation(self, full_system: Dict[str, Any]):
        executor = SandboxExecutor(full_system["sandbox_manifest"], lambda *args: None)

        valid, _ = executor.validate_code(
            "result = api.server('playwright').navigate(url='http://example.com')"
        )
        assert valid

        valid, _ = executor.validate_code("import os\nos.system('rm -rf /')")
        assert not valid

    def test_complete_user_workflow(self, full_system: Dict[str, Any]):
        registry = full_system["registry"]
        query = ManifestQuery(registry)
        sandbox_manifest = full_system["sandbox_manifest"]
        access_control = NamespaceAccessControl(sandbox_manifest)

        search_results = query.search("hash", namespace="secure")
        assert len(search_results["results"]) >= 1

        allowed, _ = access_control.can_access("secure", "crypto")
        assert allowed

        tools, _ = access_control.get_allowed_tools("secure", "crypto")
        assert "crypto__hash" in tools


class TestErrorRecovery:
    """Tests for error recovery and graceful degradation."""

    def test_registry_handles_invalid_tools_gracefully(self):
        registry = CapabilityRegistry()
        manifest = registry.build(
            {
                "valid": [{"name": "tool1"}],
                "invalid": ["not a dict", {"no_name": "value"}, {"name": "valid"}],
            }
        )

        assert manifest["servers"]["valid"]["tool_count"] == 1
        assert manifest["servers"]["invalid"]["tool_count"] == 1

    def test_query_handles_empty_manifest(self):
        registry = CapabilityRegistry()
        query = ManifestQuery(registry)

        results = query.search("anything")
        assert "error" in results

    def test_access_control_handles_missing_namespace(self):
        manifest = AccessControlConfig(servers={}, namespaces={})
        access_control = NamespaceAccessControl(manifest)

        allowed, error = access_control.can_access("nonexistent", "server")
        assert not allowed
        assert "not found" in error

    @pytest.mark.asyncio
    async def test_executor_returns_structured_error(self):
        manifest = AccessControlConfig(servers={}, namespaces={})
        executor = SandboxExecutor(manifest, lambda *args: None)

        result = await executor.execute("import os", "namespace")

        assert result["status"] == "error"
        assert result["result"] is None
        assert "traceback" in result
        assert "execution_time_ms" in result

    def test_event_hook_continues_after_error(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        call_order = []

        def hook1():
            call_order.append(1)
            raise RuntimeError("Error in hook1")

        def hook2():
            call_order.append(2)

        manager.register_hook("startup", hook1)
        manager.register_hook("startup", hook2)

        manager.trigger("startup")

        assert call_order == [1, 2]
