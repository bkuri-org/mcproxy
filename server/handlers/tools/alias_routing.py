"""Alias routing: short-name calls (aliases + core tools) resolve to targets.

Extracted from router.py per review (SRP): the router owns dispatch and
direct-call routing; this module owns the alias/core-tool resolution path
that pre-promotes always-exposed aliases and delegates to the direct-call
handler in the router.
"""

from typing import Any, Callable, Dict, Optional

from aliases import resolve_alias_target, resolve_core_tool
from exposure import get_exposure_settings, promotion_state
from logging_config import get_logger
from manifest import CapabilityRegistry

logger = get_logger(__name__)


async def resolve_alias_call(
    msg_id: Any,
    tool_name: str,
    arguments: Dict[str, Any],
    namespace: Optional[str],
    capability_registry: Optional[CapabilityRegistry],
    session_id: Optional[str],
    mcproxy_config: Optional[Dict[str, Any]],
    tool_executor: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """Route a core-tool or alias call to its target; None when not one.

    Core tools (execute/search/help) rewrite to dispatch meta-actions.
    Manual + auto aliases rewrite to the full server__tool name and execute
    directly (aliases are always exposed — no dormant teach error).
    """
    rewritten = resolve_core_tool(tool_name, arguments)
    if rewritten:
        return None  # handled by the dispatch route below

    # Review guard: null-check config before alias settings access.
    if not mcproxy_config:
        return None

    target = resolve_alias_target(
        tool_name, mcproxy_config, capability_registry, namespace
    )
    if not target:
        return None

    # Aliases are always exposed: pre-promote so check_direct_call passes.
    seat = session_id or "anonymous"
    exposure_settings = get_exposure_settings(mcproxy_config, namespace)
    if exposure_settings:
        promotion_state.promote(seat, target, exposure_settings["cap"])
    logger.info(f"[ALIASES] {tool_name} → {target} (seat={seat})")

    # Local import: router imports this module at load time (cycle break).
    from .router import _handle_direct_call

    return await _handle_direct_call(
        msg_id,
        target,
        arguments,
        namespace=namespace,
        capability_registry=capability_registry,
        tool_executor=tool_executor,
        session_id=session_id,
        mcproxy_config=mcproxy_config,
    )
