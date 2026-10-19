"""Tests for the exposure policy layer (dormant shelf + promotion).

Covers: tools/list filtering + deterministic ordering, per-seat LRU
promotion, caps/overflow, teach-error contract, cacheScope/ttlMs fields,
and default-off behavior.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from exposure import (
    DEFAULT_DORMANT_LINES,
    DEFAULT_PROMOTED_CAP,
    DORMANT_TOOL_NAME,
    PromotionState,
    apply_exposure,
    check_direct_call,
    get_exposure_settings,
    promote_from_inspect,
    promotion_state,
    shelf_text,
)
from server.handlers import handle_tools_list


# ============================================================================
# Fixtures
# ============================================================================


def make_manifest() -> Dict[str, List[Dict[str, Any]]]:
    """Two servers, three tools each — intentionally unsorted."""
    return {
        "tools_by_server": {
            "zeta": [
                {
                    "name": "zsearch",
                    "description": "Search zeta things deeply and thoroughly",
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
                {
                    "name": "alogin",
                    "description": "Log into zeta",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ],
            "alpha": [
                {
                    "name": "run",
                    "description": "Run the alpha pipeline with full verbose diagnostics",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                        "required": ["mode"],
                    },
                },
                {
                    "name": "stop",
                    "description": "Stop it",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ],
        }
    }


def make_registry(manifest=None):
    manifest = manifest if manifest is not None else make_manifest()
    return SimpleNamespace(
        _manifest=manifest,
        resolve_namespace_to_servers=lambda ns: (["alpha", "zeta"], None),
    )


EXPOSURE_CFG = {
    "exposure": {
        "enabled": True,
        "promoted_cap_per_session": DEFAULT_PROMOTED_CAP,
        "dormant_max_lines": DEFAULT_DORMANT_LINES,
        "namespaces": {"thinking": {"exposed": ["alpha"]}},
    }
}


def list_tools(registry, namespace="thinking", config=EXPOSURE_CFG, session_id="s1"):
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


@pytest.fixture(autouse=True)
def clean_promotion_state():
    promotion_state.forget_seat("s1")
    promotion_state.forget_seat("anonymous")
    yield
    promotion_state.forget_seat("s1")
    promotion_state.forget_seat("anonymous")


# ============================================================================
# Config parsing
# ============================================================================


def test_exposure_off_when_no_block():
    assert get_exposure_settings({}, "thinking") is None


def test_exposure_off_when_disabled():
    cfg = {"exposure": {"enabled": False, "namespaces": {"thinking": {"exposed": ["a"]}}}}
    assert get_exposure_settings(cfg, "thinking") is None


def test_exposure_off_for_unlisted_namespace():
    assert get_exposure_settings(EXPOSURE_CFG, "financial") is None


def test_exposure_off_without_namespace():
    assert get_exposure_settings(EXPOSURE_CFG, None) is None


def test_exposure_settings_values():
    s = get_exposure_settings(EXPOSURE_CFG, "thinking")
    assert s == {"exposed": ["alpha"], "cap": 15, "dormant_lines": 50}


# ============================================================================
# tools/list filtering + deterministic ordering
# ============================================================================


def test_exposure_off_returns_all_tools_unchanged():
    resp = list_tools(make_registry(), config={})
    tools = resp["result"]["tools"]
    # dispatch + 4 upstream (no exposure extras)
    assert [t["name"] for t in tools] == [
        "dispatch",
        "zeta__zsearch",
        "zeta__alogin",
        "alpha__run",
        "alpha__stop",
    ]
    assert "dormant" not in resp["result"]
    assert "ttlMs" not in resp["result"]
    assert "cacheScope" not in resp["result"]


def test_exposure_filters_and_orders_deterministically():
    resp = list_tools(make_registry())
    tools = resp["result"]["tools"]
    names = [t["name"] for t in tools]
    # dispatch first, exposed server tools sorted, synthetic dormant last
    assert names == ["dispatch", "alpha__run", "alpha__stop", DORMANT_TOOL_NAME]
    # exposed entries carry full schemas
    assert tools[1]["inputSchema"]["required"] == ["mode"]
    # dormant shelf is structured + rides as a synthetic tool entry
    dormant = resp["result"]["dormant"]
    assert [d["name"] for d in dormant] == ["zeta__alogin", "zeta__zsearch"]
    for d in dormant:
        assert d["server"] == "zeta"
        assert d["gist"] and d["health"] in ("ok", "degraded", "unknown")
    shelf_entry = tools[-1]
    assert "zeta__zsearch" in shelf_entry["description"]


def test_ordering_independent_of_manifest_insertion_order():
    manifest = make_manifest()
    manifest["tools_by_server"] = dict(
        reversed(list(manifest["tools_by_server"].items()))
    )
    first = list_tools(make_registry())
    second = list_tools(make_registry(), )
    shuffled = list_tools(make_registry_with_reversed(manifest))
    assert [t["name"] for t in first["result"]["tools"]] == [
        t["name"] for t in shuffled["result"]["tools"]
    ]
    assert [t["name"] for t in second["result"]["tools"]] == [
        t["name"] for t in shuffled["result"]["tools"]
    ]


def make_registry_with_reversed(manifest):
    m2 = {"tools_by_server": {k: list(reversed(v)) for k, v in manifest["tools_by_server"].items()}}
    m2["tools_by_server"] = dict(reversed(list(m2["tools_by_server"].items())))
    return make_registry(m2)


def test_promoted_tool_gets_full_schema():
    promotion_state.promote("s1", "zeta__zsearch", 15)
    resp = list_tools(make_registry())
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["dispatch", "alpha__run", "alpha__stop", "zeta__zsearch", DORMANT_TOOL_NAME]
    # promoted tool is no longer on the shelf
    assert [d["name"] for d in resp["result"]["dormant"]] == ["zeta__alogin"]


def test_promotion_state_is_seat_scoped():
    promotion_state.promote("s1", "zeta__zsearch", 15)
    resp_other = list_tools(make_registry(), session_id="s2")
    names_other = [t["name"] for t in resp_other["result"]["tools"]]
    assert "zeta__zsearch" not in names_other


def test_cache_fields_seat_scoped():
    resp = list_tools(make_registry(), session_id="seat-42")
    assert resp["result"]["ttlMs"] == 60000
    assert resp["result"]["cacheScope"] == "seat:seat-42"


# ============================================================================
# Caps + overflow
# ============================================================================


def test_dormant_cap_and_overflow():
    cfg = {
        "exposure": {
            "enabled": True,
            "namespaces": {"thinking": {"exposed": ["alpha"]}},
            "dormant_max_lines": 1,
        }
    }
    resp = list_tools(make_registry(), config=cfg)
    dormant = resp["result"]["dormant"]
    assert len(dormant) == 1  # sorted order → zeta__alogin kept
    assert dormant[0]["name"] == "zeta__alogin"
    overflow = resp["result"]["dormant_overflow"]
    assert overflow["per_server"] == {"zeta": 1}
    assert "search" in overflow["hint"]


def test_gist_capped_at_ten_words():
    _, dormant, _ = apply_exposure(
        make_manifest(), ["zeta"], {"exposed": [], "cap": 15, "dormant_lines": 50},
        "s1", lambda srv, name: name,
    )
    for d in dormant:
        assert len(d["gist"].split()) <= 10


def test_shelf_text_overflow_hint():
    text = shelf_text(
        [{"name": "a__b", "gist": "g", "health": "ok"}],
        {"per_server": {"a": 2}, "hint": "+2 more hidden — use dispatch(action='search')"},
    )
    assert "a__b" in text and "search" in text


# ============================================================================
# Promotion state (LRU)
# ============================================================================


def test_promotion_lru_cap_and_refresh():
    state = PromotionState(cap=3)
    for name in ["a", "b", "c", "d"]:
        state.promote("s", name, 3)
    assert state.promoted("s") == {"b", "c", "d"}  # oldest (a) evicted
    state.promote("s", "b", 3)  # LRU refresh
    state.promote("s", "e", 3)  # evicts c (now oldest)
    assert state.promoted("s") == {"b", "d", "e"}


# ============================================================================
# Direct-call gate (teach error contract)
# ============================================================================


def test_direct_dormant_call_teach_error_and_promotes():
    err = check_direct_call(7, EXPOSURE_CFG, "thinking", "s1", "zeta__zsearch")
    assert err["id"] == 7
    assert err["error"]["code"] == -32000
    assert "zeta__zsearch" in err["error"]["message"]
    assert "dormant" in err["error"]["message"]
    assert "search/describe" in err["error"]["message"]
    assert err["error"]["data"]["tool"] == "zeta__zsearch"
    # auto-promoted: second attempt passes
    assert (
        check_direct_call(8, EXPOSURE_CFG, "thinking", "s1", "zeta__zsearch") is None
    )
    # ...and the next tools/list exposes it
    resp = list_tools(make_registry())
    assert "zeta__zsearch" in [t["name"] for t in resp["result"]["tools"]]


def test_direct_exposed_call_passes():
    assert check_direct_call(1, EXPOSURE_CFG, "thinking", "s1", "alpha__run") is None


def test_direct_call_noop_when_exposure_off():
    assert check_direct_call(1, {}, "thinking", "s1", "zeta__zsearch") is None


# ============================================================================
# Describe promotion
# ============================================================================


def test_describe_promotes_dormant_tool():
    registry = make_registry()
    promote_from_inspect(
        EXPOSURE_CFG, "thinking", "s1", {"server": "zeta", "tool": "alogin"}, registry
    )
    resp = list_tools(registry)
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "zeta__alogin" in names
    assert "zeta__alogin" not in [d["name"] for d in resp["result"]["dormant"]]


def test_describe_whole_server_promotes_all_its_tools():
    registry = make_registry()
    promote_from_inspect(EXPOSURE_CFG, "thinking", "s1", {"server": "zeta"}, registry)
    resp = list_tools(registry)
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "zeta__alogin" in names and "zeta__zsearch" in names


def test_describe_noop_when_exposure_off():
    # must not raise and must not promote
    promote_from_inspect({}, "thinking", "s1", {"server": "zeta"}, make_registry())
    assert promotion_state.promoted("s1") == set()
