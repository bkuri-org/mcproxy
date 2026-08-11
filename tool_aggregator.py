"""Tool aggregation and prefixing for MCProxy.

Aggregates tools from multiple MCP servers and adds server name prefixes.
Supports per-server strip_tool_prefix and tool_prefix options for
normalizing tool names from upstream servers.
"""

from typing import Any, Dict, List, Optional

from logging_config import get_logger
from utils.tools import normalize_tool

logger = get_logger(__name__)

# Per-server prefix config: {server_name: {strip_tool_prefix, tool_prefix}}
_server_prefix_configs: Dict[str, Dict[str, Optional[str]]] = {}


def set_prefix_configs(configs: Dict[str, Dict[str, Optional[str]]]) -> None:
    """Set per-server tool prefix/strip configurations.

    Args:
        configs: Dict mapping server_name to {strip_tool_prefix, tool_prefix}
    """
    global _server_prefix_configs
    _server_prefix_configs = configs


def get_prefix_configs() -> Dict[str, Dict[str, Optional[str]]]:
    """Get current per-server prefix configurations."""
    return _server_prefix_configs


def transform_tool_name(server_name: str, tool_name: str) -> str:
    """Apply strip and prefix transforms to a tool name.

    Args:
        server_name: Name of the MCP server
        tool_name: Original tool name from the upstream server

    Returns:
        Transformed tool name (strip applied, then prefix applied)
    """
    config = _server_prefix_configs.get(server_name, {})

    strip_prefix = config.get("strip_tool_prefix")
    if strip_prefix and tool_name.startswith(strip_prefix):
        tool_name = tool_name[len(strip_prefix):]

    tool_prefix = config.get("tool_prefix")
    if tool_prefix:
        tool_name = f"{tool_prefix}{tool_name}"

    return tool_name


def untransform_tool_name(server_name: str, transformed_name: str) -> str:
    """Reverse the strip/prefix transforms to recover the original tool name.

    Args:
        server_name: Name of the MCP server
        transformed_name: The tool name after prefix stripping/aliasing

    Returns:
        Original tool name as registered on the upstream server
    """
    config = _server_prefix_configs.get(server_name, {})

    # Reverse: strip custom prefix, re-add stripped prefix
    tool_prefix = config.get("tool_prefix")
    if tool_prefix and transformed_name.startswith(tool_prefix):
        transformed_name = transformed_name[len(tool_prefix):]

    strip_prefix = config.get("strip_tool_prefix")
    if strip_prefix:
        transformed_name = f"{strip_prefix}{transformed_name}"

    return transformed_name


def prefix_tool_name(server_name: str, tool_name: str) -> str:
    """Prefix tool name with server name.

    Format: {server_name}__{tool_name}
    Applies strip_tool_prefix and tool_prefix transforms before adding
    the server prefix.

    Args:
        server_name: Name of the MCP server
        tool_name: Original tool name

    Returns:
        Prefixed tool name
    """
    transformed = transform_tool_name(server_name, tool_name)
    return f"{server_name}__{transformed}"


def aggregate_tools(
    servers_tools: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Aggregate tools from all servers with prefixed names.

    Args:
        servers_tools: Dict mapping server name to list of tools from that server

    Returns:
        List of all tools with prefixed names
    """
    aggregated: List[Dict[str, Any]] = []
    seen_names: set = set()

    for server_name, tools in servers_tools.items():
        for tool in tools:
            try:
                normalized = normalize_tool(tool)
            except ValueError:
                # Preserve original log-and-continue error signaling
                logger.warning(f"Invalid tool format from server {server_name}: {tool}")
                continue

            original_name = normalized["name"]
            prefixed_name = prefix_tool_name(server_name, original_name)

            if prefixed_name in seen_names:
                logger.warning(
                    f"Duplicate tool name '{prefixed_name}' from server {server_name}"
                )
                continue

            seen_names.add(prefixed_name)

            # normalize_tool returns a new dict; safe to mutate directly
            prefixed_tool = normalized
            prefixed_tool["name"] = prefixed_name
            prefixed_tool["_original_name"] = original_name
            prefixed_tool["_server"] = server_name
            # Track the transformed (visible) name after strip/prefix
            prefixed_tool["_transformed_name"] = transform_tool_name(
                server_name, original_name
            )

    # NOTE: utils/tools.py must be created with normalize_tool(tool: dict) -> dict
    # Contract: returns a new dict, never mutates its argument.
    # Raises ValueError for: non-dict input, missing "name" key,
    #   non-string "name", empty-string "name" (strictest superset of
    #   both tool_aggregator.py and manifest/registry.py validations).
    # Error signaling is intentionally unified to raise ValueError;
    # callers convert to their preferred pattern (log-and-continue here,
    # raise-or-sentinel in registry).  Each divergent rejection case has
    #   a regression test pinned in tests/test_normalize_tool.py.

            aggregated.append(prefixed_tool)

    logger.debug(
        f"Aggregated {len(aggregated)} tools from {len(servers_tools)} servers"
    )
    return aggregated


def parse_prefixed_tool_name(prefixed_name: str) -> tuple:
    """Parse a prefixed tool name into server and tool components.

    Args:
        prefixed_name: Tool name in format {server}__{tool}

    Returns:
        Tuple of (server_name, tool_name)

    Raises:
        ValueError: If name format is invalid
    """
    parts = prefixed_name.split("__", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid tool name format: {prefixed_name}")
    return parts[0], parts[1]
