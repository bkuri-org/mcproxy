"""Tests for the alias layer (core tools + manual/auto aliases).

Covers: config parsing, alias set construction (manual wins, collision skip,
usage ranking, cap), core-tool → dispatch rewrite, alias call resolution,
tools/list integration, and the uncapped promotion behavior when the alias
layer is active.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from aliases import (
    CORE_TOOL_DEFS,
    UNLIMITED_PROMOTION_CAP,
    alias_state,
    build_aliases,
    get_alias_settings,
    resolve_alias,
    resolve_core_tool,
)
from exposure import get_exposure_settings, promotion_state
from server.handlers import handle_tools_list
from server.handlers.tools import handle_tools_call


# ============================================================================
# Fixtures
# ============================================================================


def make_manifest() -> Dict[str, List[Dict[str, Any]]]:
    """Two servers; 'search' exists on both (collision), 'run' only on alpha."""
    return {
        "tools_by_server": {
            "alpha": [
                {
                    "name": "run",
                    "description": "Run the alpha pipeline",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                        "required": ["mode"],
                    },
                },
                {
                    "name": "search",
                    "description": "Search alpha things",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            ],
            "imo": [
                {
                    "name": "recall",
                    "description": "Recall a memory by rid",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"rid": {"type": "string"}},
                        "required": ["rid"],
                    },
                },
                {
                    "name": "remember",
                    "description": "Store a durable fact",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"fact": {"type": "string"}},
                        "required": ["fact"],
                    },
                },
            ],
            "zeta": [
                {
                    "name": "search",
                    "description": "Search zeta things",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            ],
        }
    }


def make_registry(manifest=None):
    manifest = manifest if manifest is not None else make_manifest()
    return SimpleNamespace(
        _manifest=manifest,
        resolve_namespace_to_servers=lambda ns: (["alpha", "imo", "zeta"], None),
    )


ALIAS_CFG = {
    "exposure": {
        "enabled": True,
        "namespaces": {"thinking": {"exposed": []}},
    },
    "aliases": {
        "manual": {"recall": "imo__recall"},
        "auto": True,
        "max_total": 4,
        "warn_threshold": 8,
    },
}


@pytest.fixture(autouse=True)
def clean_state():
    alias_state.reset()
    promotion_state.forget_seat("s1")
    promotion_state.forget_seat("anonymous")
    yield
    alias_state.reset()
    promotion_state.forget_seat("s1")
    promotion_state.forget_seat("anonymous")


def list_tools(registry, namespace="thinking", config=ALIAS_CFG, session_id="s1"):
    return asyncio.run(
        handle_tools_list(
            1,
            namespace=namespace,
            capability_registry=registry,
            params={},
            config=config,
            session_id=session_id,
        )
    )


def tool_names(result):
    return [t["name"] for t in result["result"]["tools"]]


# ============================================================================
# Config parsing
# ============================================================================


def test_aliases_off_when_no_block():
    assert get_alias_settings({}) is None
    assert get_alias_settings({"exposure": {"enabled": True}}) is None


def test_alias_settings_parse_and_defaults():
    s = get_alias_settings({"aliases": {"manual": {"recall": "imo__recall"}}})
    assert s["manual"] == {"recall": "imo__recall"}
    assert s["auto"] is True
    assert s["max_total"] == 10
    assert s["warn_threshold"] == 8


# ============================================================================
# Alias set construction
# ============================================================================


def test_manual_alias_and_core_tools_in_tools_list():
    result = list_tools(make_registry())
    names = tool_names(result)
    assert "execute" in names and "search" in names and "help" in names
    assert "recall" in names
    recall = next(t for t in result["result"]["tools"] if t["name"] == "recall")
    assert recall["inputSchema"]["required"] == ["rid"]
    assert "imo__recall" in recall["description"]


def test_no_alias_block_means_unchanged_tools_list():
    cfg = {"exposure": {"enabled": True, "namespaces": {"thinking": {"exposed": []}}}}
    result = list_tools(make_registry(), config=cfg)
    names = tool_names(result)
    assert "execute" not in names and "recall" not in names


def test_unused_tools_never_auto_exposed():
    result = list_tools(make_registry())
    names = tool_names(result)
    assert "run" not in names  # never called → stays dormant
    assert "remember" not in names


def test_auto_alias_after_usage_and_ranking():
    # Two direct calls on alpha__run, one on alpha__search
    alias_state.record("alpha__run")
    alias_state.record("alpha__run")
    alias_state.record("alpha__search")
    registry = make_registry()
    result = list_tools(registry)
    names = tool_names(result)
    # Used tools get auto aliases, ranked by usage (run first)
    assert "run" in names
    settings = get_alias_settings(ALIAS_CFG)
    _entries, alias_map = build_aliases(
        make_manifest(), ["alpha", "imo", "zeta"], settings, alias_state.usage()
    )
    assert alias_map.get("run") == "alpha__run"
    assert "search" not in alias_map


def test_collision_short_name_skipped_for_auto_alias():
    alias_state.record("alpha__search")
    alias_state.record("zeta__search")
    settings = get_alias_settings(ALIAS_CFG)
    entries, alias_map = build_aliases(
        make_manifest(), None, settings, alias_state.usage()
    )
    # 'search' exists on two servers → ambiguous, must not appear as alias
    assert "search" not in alias_map
    assert all(e["name"] != "search" for e in entries)


def test_unambiguous_used_tool_gets_auto_alias():
    alias_state.record("alpha__run")
    settings = get_alias_settings(ALIAS_CFG)
    entries, alias_map = build_aliases(
        make_manifest(), None, settings, alias_state.usage()
    )
    assert alias_map.get("run") == "alpha__run"
    run = next(e for e in entries if e["name"] == "run")
    assert run["inputSchema"]["required"] == ["mode"]


def test_manual_wins_over_auto():
    # imo__remember used AND manually aliased: manual entry, single entry
    alias_state.record("imo__remember")
    cfg = dict(ALIAS_CFG)
    cfg["aliases"] = {
        "manual": {"remember": "imo__remember"},
        "auto": True,
        "max_total": 4,
        "warn_threshold": 8,
    }
    settings = get_alias_settings(cfg)
    entries, alias_map = build_aliases(
        make_manifest(), None, settings, alias_state.usage()
    )
    named = [e["name"] for e in entries if e["name"] == "remember"]
    assert len(named) == 1
    assert alias_map["remember"] == "imo__remember"


def test_max_total_cap_includes_manual():
    alias_state.record("alpha__run")
    alias_state.record("imo__remember")
    cfg = {
        "aliases": {
            "manual": {"recall": "imo__recall", "remember": "imo__remember"},
            "auto": True,
            "max_total": 2,
            "warn_threshold": 99,
        }
    }
    settings = get_alias_settings(cfg)
    _entries, alias_map = build_aliases(
        make_manifest(), None, settings, alias_state.usage()
    )
    assert len(alias_map) <= 2
    assert "recall" in alias_map and "remember" in alias_map


def test_unknown_manual_target_skipped():
    cfg = {
        "aliases": {
            "manual": {"ghost": "nope__missing", "recall": "imo__recall"},
            "auto": False,
            "max_total": 10,
            "warn_threshold": 99,
        }
    }
    settings = get_alias_settings(cfg)
    _entries, alias_map = build_aliases(
        make_manifest(), None, settings, {}
    )
    assert "ghost" not in alias_map
    assert alias_map["recall"] == "imo__recall"


def test_reserved_names_never_aliased():
    # A used tool literally named 'help' must not shadow the core help tool.
    manifest = {"tools_by_server": {"foo": [{"name": "help", "description": "x", "inputSchema": {}}]}}
    alias_state.record("foo__help")
    settings = get_alias_settings(ALIAS_CFG)
    _entries, alias_map = build_aliases(manifest, None, settings, alias_state.usage())
    assert "help" not in alias_map


# ============================================================================
# Core tools + call-side resolution
# ============================================================================


def test_resolve_core_tool_rewrites_to_dispatch():
    tool, args = resolve_core_tool("execute", {"code": "1+1"})
    assert tool == "dispatch" and args["action"] == "execute"

    tool, args = resolve_core_tool("search", {"query": "weather"})
    assert tool == "dispatch" and args["action"] == "search"

    # help merged with inspect: server/tool args → inspect
    tool, args = resolve_core_tool("help", {"server": "imo", "tool": "recall"})
    assert tool == "dispatch" and args["action"] == "inspect"

    tool, args = resolve_core_tool("help", {"topic": "sandbox"})
    assert tool == "dispatch" and args["action"] == "help"


def test_resolve_core_tool_ignores_non_core():
    assert resolve_core_tool("recall", {}) is None


def test_resolve_alias_manual():
    target = resolve_alias("recall", ALIAS_CFG, make_manifest(), None)
    assert target == "imo__recall"


def test_resolve_alias_auto_uses_usage_counts():
    alias_state.record("alpha__run")
    target = resolve_alias("run", ALIAS_CFG, make_manifest(), None)
    assert target == "alpha__run"


def test_resolve_alias_off_without_config():
    assert resolve_alias("recall", {}, make_manifest(), None) is None


def _call(registry, name, arguments, config=ALIAS_CFG, executor=None):
    async def run():
        return await handle_tools_call(
            7,
            {"name": name, "arguments": arguments},
            namespace="thinking",
            session_id="s1",
            capability_registry=registry,
            tool_executor=executor,
            mcproxy_config=config,
        )

    return asyncio.run(run())


def test_core_execute_call_routes_to_dispatch_action():
    registry = make_registry()

    async def fake_exec(server, tool, args):
        assert (server, tool) == ("imo", "recall")
        return [{"type": "text", "text": "ok"}]

    result = _call(registry, "recall", {"rid": "r1"}, executor=fake_exec)
    assert result["result"]["content"][0]["text"] == "ok"
    # The alias call promoted + recorded the target
    assert alias_state.usage().get("imo__recall", 0) == 1
    assert "imo__recall" in promotion_state.promoted("s1")


def test_alias_call_works_without_dormant_teach_error():
    """Aliases are always exposed: first call executes, no dormant error."""
    registry = make_registry()

    async def fake_exec(server, tool, args):
        return {"content": [{"type": "text", "text": "done"}]}

    result = _call(registry, "recall", {"rid": "r2"}, executor=fake_exec)
    assert "error" not in result


def test_unknown_short_name_still_errors():
    registry = make_registry()
    result = _call(registry, "nope", {}, executor=None)
    assert result["error"]["code"] == -32601


def test_alias_call_with_null_config_errors_and_promotes_nothing():
    """Router null-guards mcproxy_config before alias settings access."""
    registry = make_registry()
    result = _call(registry, "recall", {"rid": "r1"}, config=None, executor=None)
    assert result["error"]["code"] == -32601
    assert "imo__recall" not in promotion_state.promoted("s1")


def test_blocked_dormant_tool_is_never_recorded():
    """alias_state.record runs only for calls that pass the exposure gate."""
    from server.handlers.tools.router import _handle_direct_call

    registry = make_registry()
    cfg = {"exposure": {"enabled": True,
                        "namespaces": {"thinking": {"exposed": []}}}}

    async def run():
        return await _handle_direct_call(
            7, "alpha__run", {}, namespace="thinking",
            capability_registry=registry, tool_executor=None,
            session_id="s2", mcproxy_config=cfg,
        )

    result = asyncio.run(run())
    # Dormant tool: teach error, never recorded for auto-alias ranking
    assert result["error"]["data"]["status"] == "dormant"
    assert alias_state.usage().get("alpha__run", 0) == 0

# ============================================================================
# Exposure policy interaction
# ============================================================================


def test_promotion_cap_unlimited_when_alias_layer_active():
    s = get_exposure_settings(ALIAS_CFG, "thinking")
    assert s["cap"] == UNLIMITED_PROMOTION_CAP


def test_promotion_cap_normal_without_alias_block():
    cfg = {"exposure": {"enabled": True, "promoted_cap_per_session": 15,
                        "namespaces": {"thinking": {"exposed": []}}}}
    s = get_exposure_settings(cfg, "thinking")
    assert s["cap"] == 15


# ============================================================================
# Core tool schema sanity
# ============================================================================


def test_core_tool_defs_shape():
    assert [d["name"] for d in CORE_TOOL_DEFS] == ["execute", "search", "help"]
    for d in CORE_TOOL_DEFS:
        assert d["inputSchema"]["type"] == "object"
