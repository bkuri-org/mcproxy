"""Alias layer: short, intuitive tool names over the full server__tool catalog.

Agents see a short list — core tools (mcproxy's own dispatch actions surfaced
as individually-named tools), manual aliases from config, and auto-generated
aliases for frequently-used tools — instead of the ~400-tool raw catalog.
Everything else stays on the dormant shelf (exposure.py).

Config-gated: no "aliases" block in mcproxy.json means the alias layer is off
and behavior is unchanged. Design: task mcproxy-alias-system (261026).
"""

import threading
from typing import Any, Dict, List, Optional, Tuple

from logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_TOTAL = 10
DEFAULT_WARN_THRESHOLD = 8

# When the alias layer is active it IS the cap on the exposed list — per-seat
# promotion no longer needs its own limit.
UNLIMITED_PROMOTION_CAP = 10**6

# Reserved short names: core tools + dispatch meta-tools + the synthetic
# dormant-shelf tool. Auto aliases never take these.
CORE_TOOLS = ("execute", "search", "help")
RESERVED_SHORT_NAMES = set(CORE_TOOLS) | {
    "dispatch",
    "mcproxy",
    "dormant",
    "refresh",
    "trace",
    "inspect",
}

CORE_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "execute",
        "description": (
            "Call any MCP tool directly with Python: "
            "api.server('name').tool_name(param='value'). Keyword arguments only; "
            "unpack dicts with **."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code, e.g. api.server('wikipedia').search(query='Python')",
                },
                "timeout_secs": {"type": "integer", "description": "Execution timeout in seconds"},
                "retries": {"type": "integer", "default": 0},
                "trace": {"type": "boolean", "default": False},
            },
            "required": ["code"],
        },
    },
    {
        "name": "search",
        "description": "Find tools across all servers by keyword. Returns descriptions; use help(server=..., tool=...) for schemas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "help",
        "description": (
            "Understand a tool or server. Pass server (and optionally tool) for "
            "schemas; pass topic for docs (e.g. topic='sandbox')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Server name"},
                "tool": {"type": "string", "description": "Tool name (with server)"},
                "topic": {"type": "string", "description": "Help topic (e.g. 'sandbox')"},
            },
        },
    },
]


# ============================================================================
# Usage tracking (in-memory, process-local)
# ============================================================================


class AliasState:
    """Usage counts per full tool name (server__tool), thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: Dict[str, int] = {}

    def record(self, full_name: str) -> None:
        with self._lock:
            self._usage[full_name] = self._usage.get(full_name, 0) + 1

    def usage(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._usage)

    def reset(self) -> None:
        with self._lock:
            self._usage.clear()


alias_state = AliasState()


# ============================================================================
# Config
# ============================================================================


def get_alias_settings(config: Optional[dict]) -> Optional[dict]:
    """Return alias settings, or None when the aliases block is absent.

    None = alias layer off; tools/list and tools/call behave exactly as before.
    """
    if not config or not isinstance(config.get("aliases"), dict):
        return None
    cfg = config["aliases"]
    manual_raw = cfg.get("manual") or {}
    manual = {str(k): str(v) for k, v in manual_raw.items() if v}
    return {
        "manual": manual,
        "auto": bool(cfg.get("auto", True)),
        "max_total": max(1, int(cfg.get("max_total", DEFAULT_MAX_TOTAL))),
        "warn_threshold": int(cfg.get("warn_threshold", DEFAULT_WARN_THRESHOLD)),
    }


# ============================================================================
# Alias set construction (tools/list side)
# ============================================================================


def _alias_targets(
    manifest: Dict[str, Any],
    allowed_servers: Optional[List[str]],
    settings: dict,
    usage: Dict[str, int],
) -> Dict[str, str]:
    """Compute alias → full target name (manual first, then auto)."""
    tools_by_server = manifest.get("tools_by_server", {})

    # Index the available tools: full_name -> tool def, and short name ->
    # number of distinct servers providing it (collision detection).
    index: Dict[str, dict] = {}
    short_name_servers: Dict[str, set] = {}
    for server_name in sorted(tools_by_server):
        if allowed_servers is not None and server_name not in allowed_servers:
            continue
        for tool in tools_by_server[server_name]:
            if not isinstance(tool, dict) or "name" not in tool:
                continue
            full_name = f"{server_name}__{tool['name']}"
            index[full_name] = tool
            short_name_servers.setdefault(tool["name"], set()).add(server_name)

    alias_map: Dict[str, str] = {}
    taken: set = set(RESERVED_SHORT_NAMES)

    # Manual aliases first — they always win over auto-generated ones.
    for alias, target in settings["manual"].items():
        if alias in taken:
            logger.warning(f"[ALIASES] Manual alias '{alias}' collides with a reserved name — skipped")
            continue
        if target not in index:
            logger.warning(f"[ALIASES] Manual alias '{alias}' → unknown target '{target}' — skipped")
            continue
        taken.add(alias)
        alias_map[alias] = target

    # Auto aliases: only for tools that have actually been used, whose short
    # name is unambiguous across servers and not already taken.
    remaining = settings["max_total"] - len(alias_map)
    if settings["auto"] and remaining > 0:
        candidates = []
        for full_name in sorted(index):
            short = full_name.split("__", 1)[1]
            if short in taken or len(short_name_servers.get(short, ())) > 1:
                continue
            count = usage.get(full_name, 0)
            if count < 1:
                continue
            candidates.append((short, full_name, count))
        candidates.sort(key=lambda c: (-c[2], c[0]))
        for short, full_name, _count in candidates[:remaining]:
            taken.add(short)
            alias_map[short] = full_name

    total = len(CORE_TOOLS) + len(alias_map)
    if total > settings["warn_threshold"]:
        logger.warning(
            f"[ALIASES] Short list ({total} tools) exceeds warn_threshold "
            f"({settings['warn_threshold']}) — consider trimming manual aliases or max_total"
        )
    return alias_map


def build_aliases(
    manifest: Dict[str, Any],
    allowed_servers: Optional[List[str]],
    settings: dict,
    usage: Dict[str, int],
) -> Tuple[List[dict], Dict[str, str]]:
    """Build the alias tool entries for tools/list.

    Deterministic: manual aliases in config key order, then auto aliases by
    (usage desc, name asc). Returns (tool entries, alias → target map).
    Core tools are separate and always exposed (CORE_TOOL_DEFS).
    """
    alias_map = _alias_targets(manifest, allowed_servers, settings, usage)
    entries = []
    for alias, target in alias_map.items():
        tool = manifest["tools_by_server"][target.split("__", 1)[0]]
        tool = next(t for t in tool if t.get("name") == target.split("__", 1)[1])
        entries.append(
            {
                "name": alias,
                "description": (
                    f"Alias for {target}: {tool.get('description', '')}".strip()
                ),
                "inputSchema": tool.get("inputSchema", {}),
            }
        )
    return entries, alias_map


def resolve_core_tool(tool_name: str, arguments: dict) -> Optional[Tuple[str, dict]]:
    """Rewrite a core-tool call into its dispatch action.

    help is merged with inspect: server/tool args → inspect, otherwise help.
    Returns (tool_name, arguments) or None when not a core tool.
    """
    if tool_name not in CORE_TOOLS:
        return None
    if tool_name == "help" and (arguments.get("server") or arguments.get("tool")):
        action = "inspect"
    else:
        action = tool_name
    return "dispatch", {**arguments, "action": action}


def resolve_alias(
    tool_name: str,
    mcproxy_config: Optional[dict],
    manifest: Dict[str, Any],
    allowed_servers: Optional[List[str]],
) -> Optional[str]:
    """Resolve a short alias name to its full target, or None.

    Covers manual aliases AND auto-generated short names: the deterministic
    alias set is recomputed from the current usage counts, so a call to a
    short name seen in tools/list resolves to the same target it listed.
    """
    settings = get_alias_settings(mcproxy_config)
    if not settings:
        return None
    alias_map = _alias_targets(manifest, allowed_servers, settings, alias_state.usage())
    return alias_map.get(tool_name)
