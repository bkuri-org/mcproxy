"""Namespace configuration utilities.

Provides DRY utilities for handling namespace configuration across the codebase.
"""

from typing import Any, Dict, List, Optional


def normalize_namespace_config(ns_config: Any) -> Dict[str, Any]:
    """Normalize namespace configuration to a standard dict format.

    Handles both list format (simple server list) and dict format
    (with servers, isolated, extends keys).

    Args:
        ns_config: Namespace configuration (list or dict)

    Returns:
        Dict with 'servers', 'isolated', 'extends' keys
    """
    if ns_config is None:
        return {"servers": [], "isolated": False, "extends": []}

    if isinstance(ns_config, list):
        return {
            "servers": list(ns_config),
            "isolated": False,
            "extends": [],
        }
    elif isinstance(ns_config, dict):
        return {
            "servers": ns_config.get("servers", []),
            "isolated": ns_config.get("isolated", False),
            "extends": ns_config.get("extends", []),
        }

    # Fallback for unexpected types
    return {"servers": [], "isolated": False, "extends": []}


def get_namespace_servers(
    ns_config: Any, all_servers: Optional[List[str]] = None
) -> List[str]:
    """Extract server names from namespace configuration.

    Args:
        ns_config: Namespace configuration (list or dict)
        all_servers: Optional list of all available servers for validation.
                     If provided, returned servers are filtered to only
                     include servers in this list.

    Returns:
        List of server names from the namespace config
    """
    normalized = normalize_namespace_config(ns_config)
    servers = normalized["servers"]

    if all_servers is not None:
        return [s for s in servers if s in all_servers]

    return servers


def normalize_tool_name(name: Any) -> str:
    """Normalize a tool name to a canonical form for rate-limit bucket keys.

    Fail-closed: raises RuntimeError for any invalid input so that
    misconfigured or malformed names never silently bypass rate limits.
    Uses explicit RuntimeError guards instead of assertions so the
    check survives Python's ``-O`` optimisation flag.

    Args:
        name: The tool name to normalize.

    Returns:
        Canonical lower-cased, stripped tool name string.

    Raises:
        RuntimeError: If *name* is not a non-empty string.
    """
    if not isinstance(name, str):
        raise RuntimeError(
            f"Tool name must be a string, got {type(name).__name__}"
        )
    normalized = name.strip().lower()
    if not normalized:
        raise RuntimeError("Tool name must be a non-empty string")
    return normalized
