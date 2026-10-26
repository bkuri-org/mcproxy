"""Exposure policy layer: exposed hot-set + dormant shelf (owner-blessed 261019).

tools/list returns the per-namespace exposed hot-set with full schemas plus a
dormant shelf (name + server + short gist + health mark). inspect/describe and
direct calls of dormant tools auto-promote them into the session's exposed set
(LRU, capped per seat). Config-gated per namespace; no exposure block (or
enabled=false) means current behavior, byte-for-byte.

Design notes: docs/design-dormant-list.md
"""

import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from logging_config import get_logger

from aliases import UNLIMITED_PROMOTION_CAP  # noqa: E402

logger = get_logger(__name__)

DORMANT_TOOL_NAME = "dormant"
DEFAULT_PROMOTED_CAP = 15
DEFAULT_DORMANT_LINES = 50
GIST_MAX_WORDS = 10


# ============================================================================
# Per-session promotion state (seat-scoped, LRU)
# ============================================================================


class PromotionState:
    """Per-seat LRU sets of promoted tool names.

    Seats are keyed by X-Session-ID (cacheScope=per-seat constraint): state
    is NEVER shared across seats. Unknown seat = "anonymous" bucket.
    """

    def __init__(self, cap: int = DEFAULT_PROMOTED_CAP) -> None:
        self._cap = cap
        self._lock = threading.Lock()
        self._seats: Dict[str, "OrderedDict[str, None]"] = {}

    def promote(self, seat: str, full_name: str, cap: Optional[int] = None) -> None:
        """Pin a tool into the seat's exposed set (LRU refresh + evict)."""
        limit = cap or self._cap
        with self._lock:
            bucket = self._seats.setdefault(seat, OrderedDict())
            bucket[full_name] = None
            bucket.move_to_end(full_name)
            while len(bucket) > limit:
                bucket.popitem(last=False)

    def promoted(self, seat: str) -> set:
        with self._lock:
            return set(self._seats.get(seat, ()))

    def forget_seat(self, seat: str) -> None:
        with self._lock:
            self._seats.pop(seat, None)


# Module singleton — same lifecycle pattern as server.health.health_tracker.
promotion_state = PromotionState()


# ============================================================================
# Config
# ============================================================================


def get_exposure_settings(config: Optional[dict], namespace: Optional[str]) -> Optional[dict]:
    """Return exposure settings for a namespace, or None when exposure is off.

    Off = no exposure block, enabled=false, no namespace given, or the
    namespace has no entry. Per-namespace opt-in; everything else unchanged.
    """
    if not config or not namespace:
        return None
    exp = config.get("exposure") or {}
    if not exp.get("enabled"):
        return None
    ns_cfg = (exp.get("namespaces") or {}).get(namespace)
    if not ns_cfg:
        return None
    return {
        "exposed": list(ns_cfg.get("exposed") or []),
        # With the alias layer active, the alias list IS the cap on the
        # exposed set — per-seat promotion is effectively uncapped.
        "cap": (
            UNLIMITED_PROMOTION_CAP
            if config and isinstance(config.get("aliases"), dict)
            else int(exp.get("promoted_cap_per_session", DEFAULT_PROMOTED_CAP))
        ),
        "dormant_lines": int(exp.get("dormant_max_lines", DEFAULT_DORMANT_LINES)),
    }


# ============================================================================
# Helpers
# ============================================================================


def _gist(description: str) -> str:
    """First <=GIST_MAX_WORDS words of a description, whitespace-collapsed."""
    words = " ".join((description or "").split()).split(" ")
    return " ".join(words[:GIST_MAX_WORDS])


def _health_mark(prefixed_name: str) -> str:
    """ok / degraded / unknown from the shared HealthTracker (no data = unknown).

    Lazy import: server/__init__ imports handlers which import this module.
    """
    from server.health import health_tracker

    metrics = health_tracker.get_metrics(prefixed_name)
    if metrics is None:
        return "unknown"
    return "ok" if metrics["success_rate"] >= 0.9 else "degraded"


def _is_exposed(prefixed: str, exposed: List[str], promoted: set) -> bool:
    """Exact full-name match, server entry ('srv' exposes srv__*), or promoted."""
    if prefixed in promoted:
        return True
    return any(prefixed == e or prefixed.startswith(e + "__") for e in exposed)


def shelf_text(dormant: List[dict], overflow: Optional[dict]) -> str:
    """Render the dormant shelf as client-visible text (one line per tool)."""
    lines = [
        "Dormant tools (inspect or call one to promote it into this session's"
        " exposed set):"
    ]
    for d in dormant:
        lines.append(f"- {d['name']} :: {d['gist']} [{d['health']}]")
    if overflow:
        counts = ", ".join(f"{s}: {n}" for s, n in sorted(overflow["per_server"].items()))
        lines.append(overflow["hint"])
        lines.append(f"Hidden per server — {counts}")
    return "\n".join(lines)


# ============================================================================
# tools/list filtering
# ============================================================================


def apply_exposure(
    manifest: Dict[str, Any],
    allowed_servers: Optional[List[str]],
    settings: dict,
    seat: str,
    transform_fn,
) -> Tuple[List[dict], List[dict], Optional[dict]]:
    """Split a namespace's tools into exposed entries + dormant shelf.

    Deterministic ordering: servers and tools sorted by name — NEVER recency.
    Returns (exposed_tool_entries, dormant_entries, overflow).
    """
    promoted = promotion_state.promoted(seat)
    tools_by_server = manifest.get("tools_by_server", {})
    exposed_entries: List[dict] = []
    dormant: List[dict] = []

    for server_name in sorted(tools_by_server):
        if allowed_servers is not None and server_name not in allowed_servers:
            continue
        server_tools = [
            t
            for t in tools_by_server[server_name]
            if isinstance(t, dict) and "name" in t
        ]
        for tool in sorted(server_tools, key=lambda t: t["name"]):
            prefixed = f"{server_name}__{transform_fn(server_name, tool['name'])}"
            if _is_exposed(prefixed, settings["exposed"], promoted):
                exposed_entries.append(
                    {
                        "name": prefixed,
                        "description": tool.get("description", ""),
                        "inputSchema": tool.get("inputSchema", {}),
                    }
                )
            else:
                dormant.append(
                    {
                        "name": prefixed,
                        "server": server_name,
                        "gist": _gist(tool.get("description", "")),
                        "health": _health_mark(prefixed),
                    }
                )

    cap = settings["dormant_lines"]
    overflow: Optional[dict] = None
    if len(dormant) > cap:
        per_server: Dict[str, int] = {}
        for d in dormant[cap:]:
            per_server[d["server"]] = per_server.get(d["server"], 0) + 1
        overflow = {
            "per_server": per_server,
            "hint": (
                f"+{len(dormant) - cap} more hidden — "
                "use dispatch(action='search') to find them"
            ),
        }
        dormant = dormant[:cap]

    return exposed_entries, dormant, overflow


# ============================================================================
# Promotion gates (used by the tools/call router)
# ============================================================================


def check_direct_call(
    msg_id: Any,
    mcproxy_config: Optional[dict],
    namespace: Optional[str],
    session_id: Optional[str],
    prefixed_name: str,
) -> Optional[dict]:
    """Gate a direct server__tool call against the exposure policy.

    Returns a teach-error response when the tool is dormant (and promotes it
    for this seat), or None when the call may proceed.
    """
    settings = get_exposure_settings(mcproxy_config, namespace)
    if not settings:
        return None
    seat = session_id or "anonymous"
    if _is_exposed(prefixed_name, settings["exposed"], promotion_state.promoted(seat)):
        return None
    promotion_state.promote(seat, prefixed_name, settings["cap"])
    logger.info(
        f"[EXPOSURE] Promoted {prefixed_name} for seat={seat} "
        f"(namespace={namespace}); call blocked with teach error"
    )
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": -32000,
            "message": (
                f"Tool '{prefixed_name}' is dormant — search/describe first. "
                "It has been promoted for this session; call it again to execute."
            ),
            "data": {
                "status": "dormant",
                "tool": prefixed_name,
                "next": "inspect(server='...', tool='...') for the schema, "
                "or call this tool again",
            },
        },
    }


def promote_from_inspect(
    mcproxy_config: Optional[dict],
    namespace: Optional[str],
    session_id: Optional[str],
    arguments: dict,
    capability_registry: Any,
) -> None:
    """Auto-pin the describe target(s) into the seat's exposed set.

    describe/inspect of a dormant tool (or whole server) promotes it. No-op
    when exposure is off for the namespace.
    """
    settings = get_exposure_settings(mcproxy_config, namespace)
    if not settings or capability_registry is None:
        return
    server_name = arguments.get("server")
    tool_name = arguments.get("tool")
    if not server_name:
        return

    manifest = capability_registry._manifest or {}
    tools_by_server = manifest.get("tools_by_server", {})
    server_tools = tools_by_server.get(server_name, [])
    seat = session_id or "anonymous"

    for tool in server_tools:
        if not isinstance(tool, dict) or "name" not in tool:
            continue
        if tool_name and tool["name"] != tool_name:
            continue
        prefixed = f"{server_name}__{tool['name']}"
        promotion_state.promote(seat, prefixed, settings["cap"])
        logger.info(f"[EXPOSURE] Describe promoted {prefixed} for seat={seat}")


def shelf_call_response(
    msg_id: Any,
    namespace: Optional[str],
    session_id: Optional[str],
    capability_registry: Any,
    mcproxy_config: Optional[dict],
    transform_fn,
) -> dict:
    """Handle a call to the synthetic 'dormant' tool: return the shelf text."""
    settings = get_exposure_settings(mcproxy_config, namespace)
    if not settings or capability_registry is None:
        text = "Exposure policy is not enabled for this connection."
    else:
        allowed, _ = capability_registry.resolve_namespace_to_servers(namespace)
        exposed, dormant, overflow = apply_exposure(
            capability_registry._manifest or {},
            allowed,
            settings,
            session_id or "anonymous",
            transform_fn,
        )
        text = shelf_text(dormant, overflow) if dormant else "No dormant tools."
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }
