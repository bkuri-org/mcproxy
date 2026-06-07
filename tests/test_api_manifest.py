"""Tests for api_manifest.py - Capability Registry, Manifest Query, and Event Hooks."""

import pytest
from datetime import datetime, timedelta
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock

from manifest import (
    CapabilityRegistry,
    ManifestQuery,
    EventHookManager,
    ManifestError,
    NamespaceInheritanceError,
)


class TestCapabilityRegistry:
    """Tests for CapabilityRegistry class."""

    def test_build_with_tools(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        manifest = registry.build(sample_servers_tools)

        assert manifest["version"] == "3.0"
        assert "generated_at" in manifest
        assert manifest["server_count"] == 4
        assert manifest["tool_count"] == 8
        assert "playwright" in manifest["servers"]
        assert "filesystem" in manifest["servers"]

    def test_build_empty_tools(self):
        registry = CapabilityRegistry()
        manifest = registry.build({})

        assert manifest["version"] == "3.0"
        assert manifest["server_count"] == 0
        assert manifest["tool_count"] == 0

    def test_build_with_invalid_tool(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        sample_servers_tools["broken"] = [
            {"invalid": "tool"},
            "not a dict",
            {"name": "valid_tool"},
        ]
        registry = CapabilityRegistry()
        manifest = registry.build(sample_servers_tools)

        assert "broken" in manifest["servers"]
        assert manifest["servers"]["broken"]["tool_count"] == 1
        assert manifest["tools_by_server"]["broken"][0]["name"] == "valid_tool"

    def test_build_extracts_categories(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        manifest = registry.build(sample_servers_tools)

        playwright_cats = manifest["servers"]["playwright"]["categories"]
        assert "Playwright" in playwright_cats

    def test_get_servers_no_namespace(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        servers = registry.get_servers()

        assert len(servers) == 4
        assert "playwright" in servers
        assert "filesystem" in servers

    def test_get_servers_with_namespace(
        self,
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
        sample_namespaces: Dict[str, Any],
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        registry.validate_inheritance(sample_namespaces)

        servers = registry.get_servers("browser")
        assert servers == ["playwright"]

        servers = registry.get_servers("admin")
        assert set(servers) == {"playwright", "filesystem", "crypto", "system"}

    def test_get_servers_empty_manifest(self):
        registry = CapabilityRegistry()
        servers = registry.get_servers()
        assert servers == []

    def test_get_tools_for_server(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        tools = registry.get_tools("playwright")

        assert len(tools) == 3
        assert tools[0]["name"] == "playwright__navigate"

    def test_get_tools_with_namespace_filter(
        self,
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
        sample_namespaces: Dict[str, Any],
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        registry.validate_inheritance(sample_namespaces)

        tools = registry.get_tools("playwright", namespace="browser")
        assert len(tools) == 3

        tools = registry.get_tools("system", namespace="browser")
        assert tools == []

    def test_validate_inheritance_valid(self, sample_namespaces: Dict[str, Any]):
        registry = CapabilityRegistry()
        warnings = registry.validate_inheritance(sample_namespaces)

        assert isinstance(warnings, list)
        assert len(warnings) >= 1
        assert any("Circular inheritance detected" in w for w in warnings)

    def test_validate_inheritance_simple_list(self):
        registry = CapabilityRegistry()
        namespaces = {
            "simple": ["server1", "server2"],
        }
        warnings = registry.validate_inheritance(namespaces)

        assert warnings == []

    def test_validate_inheritance_missing_extends_reference(self):
        registry = CapabilityRegistry()
        namespaces = {
            "child": {
                "servers": ["server1"],
                "extends": ["nonexistent"],
            },
        }

        with pytest.raises(NamespaceInheritanceError) as exc_info:
            registry.validate_inheritance(namespaces)

        assert "Missing extends reference" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)

    def test_validate_inheritance_missing_namespace_in_cycle(self):
        registry = CapabilityRegistry()
        namespaces = {
            "a": {
                "servers": ["s1"],
                "extends": ["missing_ns"],
            },
        }

        with pytest.raises(NamespaceInheritanceError):
            registry.validate_inheritance(namespaces)

    def test_resolve_namespace_simple(self, sample_namespaces: Dict[str, Any]):
        registry = CapabilityRegistry()
        registry.validate_inheritance(sample_namespaces)

        servers = registry.resolve_namespace("browser")
        assert servers == ["playwright"]

        servers = registry.resolve_namespace("security")
        assert servers == ["crypto"]

    def test_resolve_namespace_with_inheritance(
        self, sample_namespaces: Dict[str, Any]
    ):
        registry = CapabilityRegistry()
        registry.validate_inheritance(sample_namespaces)

        servers = registry.resolve_namespace("privileged")
        assert set(servers) == {"playwright", "filesystem", "system"}

    def test_resolve_namespace_not_found(self):
        registry = CapabilityRegistry()

        with pytest.raises(NamespaceInheritanceError) as exc_info:
            registry.resolve_namespace("nonexistent")

        assert "Namespace not found" in str(exc_info.value)

    def test_resolve_namespace_circular_warning(
        self, sample_namespaces: Dict[str, Any]
    ):
        registry = CapabilityRegistry()
        registry.validate_inheritance(sample_namespaces)

        servers = registry.resolve_namespace("circular_a")
        assert "playwright" in servers or "filesystem" in servers

    def test_get_extends_list(self):
        registry = CapabilityRegistry()

        assert registry._get_extends(["server1"]) == []
        assert registry._get_extends({"servers": ["s1"], "extends": ["parent"]}) == [
            "parent"
        ]
        assert registry._get_extends({}) == []
        assert registry._get_extends(None) == []

    def test_get_servers_from_ns(self):
        registry = CapabilityRegistry()

        assert registry._get_servers_from_ns(["s1", "s2"]) == ["s1", "s2"]
        assert registry._get_servers_from_ns({"servers": ["s1"]}) == ["s1"]
        assert registry._get_servers_from_ns({}) == []
        assert registry._get_servers_from_ns(None) == []


class TestManifestQuery:
    """Tests for ManifestQuery class."""

    def test_search_by_server_name(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        results = query.search("playwright")
        assert "playwright" in results["matches"]["servers"]

    def test_search_by_tool_name(
        self,
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        results = query.search("navigate")
        tool_matches = [m for m in results["matches"]["tools"] if "navigate" in m]
        assert len(tool_matches) >= 1

    def test_search_by_prefixed_tool_name(
        self,
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        # Searching by prefixed name (as seen in tools/list) should match
        results = query.search("playwright__navigate")
        tool_matches = [m for m in results["matches"]["tools"] if "navigate" in m]
        assert len(tool_matches) >= 1

    def test_search_empty_query_overview(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        # Empty query returns server list with tool counts (no matched_tools)
        results = query.search("")
        for entry in results["results"]:
            assert "categories" in entry
            assert "tools" in entry
            assert "matched_tools" not in entry

    def test_search_query_returns_tools(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        # Non-empty query returns matching tools
        results = query.search("playwright")
        for entry in results["results"]:
            if entry["server"] == "playwright":
                assert "matched_tools" in entry
                # Server match expands all tools
                assert len(entry["matched_tools"]) == 3

    def test_search_server_match_expands_all_tools(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        """When query matches a server name, all tools are returned (collapsed expansion)."""
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        results = query.search("crypto")
        crypto_entry = next(
            r for r in results["results"] if r["server"] == "crypto"
        )
        assert len(crypto_entry["matched_tools"]) == 2

    def test_search_tool_description_truncated(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        results = query.search("navigate")
        for entry in results["results"]:
            for tool in entry.get("matched_tools", []):
                if "description" in tool:
                    assert len(tool["description"]) <= 200

    def test_search_with_namespace_filter(
        self,
        sample_servers_tools: Dict[str, List[Dict[str, Any]]],
        sample_namespaces: Dict[str, Any],
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        registry.validate_inheritance(sample_namespaces)
        query = ManifestQuery(registry)

        results = query.search("crypto", namespace="browser")
        browser_servers = [r["server"] for r in results["results"]]
        assert "crypto" not in browser_servers

        results = query.search("crypto", namespace="security")
        security_servers = [r["server"] for r in results["results"]]
        assert "crypto" in security_servers

    def test_search_empty_manifest(self):
        registry = CapabilityRegistry()
        query = ManifestQuery(registry)

        results = query.search("anything")
        assert "error" in results
        assert results["results"] == []

    def test_search_fuzzy_matching(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        results = query.search("play")
        assert len(results["matches"]["servers"]) >= 1

    def test_search_no_matches(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        query = ManifestQuery(registry)

        results = query.search("zzzzzzzzz_unique_string_no_match")
        assert len(results["matches"]["servers"]) == 0

    def test_fuzzy_match_exact(self):
        from utils.fuzzy_match import fuzzy_score

        score = fuzzy_score("playwright", "playwright", 0.5)
        assert score == 1.0

    def test_fuzzy_match_substring(self):
        from utils.fuzzy_match import fuzzy_score

        score = fuzzy_score("play", "playwright", 0.5)
        assert score == 1.0

    def test_fuzzy_match_word_similarity(self):
        from utils.fuzzy_match import fuzzy_score

        score = fuzzy_score("play wright", "playwright browser", 0.4)
        assert score >= 0.4


class TestEventHookManager:
    """Tests for EventHookManager class."""

    def test_register_hook_valid(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        def callback():
            pass

        manager.register_hook("startup", callback)
        assert len(manager._hooks["startup"]) == 1

    def test_register_hook_invalid_event(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        with pytest.raises(ValueError) as exc_info:
            manager.register_hook("invalid_event", lambda: None)

        assert "Invalid event type" in str(exc_info.value)

    def test_trigger_executes_hooks(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        manager = EventHookManager(registry)

        call_count = [0]

        def callback():
            call_count[0] += 1
            return "result"

        manager.register_hook("startup", callback)
        result = manager.trigger("startup")

        assert call_count[0] == 1
        assert result["hooks_executed"] == 1

    def test_trigger_with_data(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        received_data = [None]

        def callback(data):
            received_data[0] = data
            return data

        manager.register_hook("manual", callback)
        manager.trigger("manual", {"key": "value"})

        assert received_data[0] == {"key": "value"}

    def test_trigger_hook_error_handling(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        def failing_callback():
            raise RuntimeError("Test error")

        manager.register_hook("startup", failing_callback)
        result = manager.trigger("startup")

        assert result["hooks_executed"] == 1
        assert manager._last_event["results"][0]["status"] == "error"
        assert "Test error" in manager._last_event["results"][0]["error"]

    def test_trigger_invalid_event(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        result = manager.trigger("invalid_event")

        assert "error" in result
        assert "Invalid event type" in result["error"]

    def test_trigger_config_change_invalidates_cache(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        manager = EventHookManager(registry)

        manager.trigger("config_change")

        assert registry._manifest == {}

    def test_trigger_server_health_updates_status(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        manager = EventHookManager(registry)

        manager.trigger("server_health", {"server": "playwright", "status": "degraded"})

        assert registry._manifest["servers"]["playwright"]["status"] == "degraded"

    def test_get_event_history(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        manager.trigger("startup")
        manager.trigger("manual")

        history = manager.get_event_history(limit=10)
        assert len(history) == 2

    def test_get_event_history_with_limit(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        for _ in range(5):
            manager.trigger("manual")

        history = manager.get_event_history(limit=2)
        assert len(history) == 2

    def test_get_last_event(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        manager.trigger("startup")
        manager.trigger("manual")

        last = manager.get_last_event()
        assert last["event_type"] == "manual"

    def test_clear_hooks_all(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        manager.register_hook("startup", lambda: None)
        manager.register_hook("manual", lambda: None)

        count = manager.clear_hooks()
        assert count == 2
        assert len(manager._hooks) == 0

    def test_clear_hooks_specific_event(self):
        registry = CapabilityRegistry()
        manager = EventHookManager(registry)

        manager.register_hook("startup", lambda: None)
        manager.register_hook("manual", lambda: None)

        count = manager.clear_hooks("startup")
        assert count == 1
        assert len(manager._hooks["manual"]) == 1

    def test_event_history_max_limit(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]]
    ):
        registry = CapabilityRegistry()
        registry.build(sample_servers_tools)
        manager = EventHookManager(registry)

        for i in range(150):
            manager.trigger("manual")

        assert len(manager._event_history) <= manager._max_history


class TestCapabilityRegistryCache:
    """Tests for cache functionality."""

    def test_invalidate_cache(
        self, sample_servers_tools: Dict[str, List[Dict[str, Any]]], tmp_path
    ):
        with patch("manifest.registry.CACHE_DIR", tmp_path):
            registry = CapabilityRegistry()
            registry.build(sample_servers_tools)

            registry.invalidate_cache()

            assert registry._manifest == {}

    def test_load_cache_not_exists(self, tmp_path):
        with patch("manifest.registry.CACHE_DIR", tmp_path):
            with patch("manifest.registry.CACHE_FILE", tmp_path / "manifest.json"):
                registry = CapabilityRegistry()
                result = registry.load_cache()

                assert result is None

    def test_load_cache_expired(self, tmp_path):
        with patch("manifest.registry.CACHE_DIR", tmp_path):
            cache_file = tmp_path / "manifest.json"
            old_time = datetime.utcnow() - timedelta(hours=2)
            cache_data = {
                "manifest": {"version": "3.0"},
                "namespaces": {},
                "cached_at": old_time.isoformat(),
            }

            import json

            with open(cache_file, "w") as f:
                json.dump(cache_data, f)

            with patch("manifest.registry.CACHE_FILE", cache_file):
                registry = CapabilityRegistry()
                result = registry.load_cache()

                assert result is None

    def test_cache_disabled(self, tmp_path):
        with patch("manifest.registry.CACHE_DIR", tmp_path):
            cache_file = tmp_path / "manifest.json"
            with patch("manifest.registry.CACHE_FILE", cache_file):
                registry = CapabilityRegistry()
                registry._cache_enabled = False
                registry.build({"server": [{"name": "tool"}]})

                assert not cache_file.exists()
