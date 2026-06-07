"""Parameter name normalization between snake_case and camelCase.

Agents often mix up parameter naming conventions when calling tools
(e.g., thought_number vs thoughtNumber). This module detects the
convention used by a tool's schema and normalizes incoming parameters
to match.

Caches detection results per (server, tool) pair for <5ms overhead.
"""

import re
from typing import Any, Dict, Optional, Set, Tuple

from logging_config import get_logger

logger = get_logger(__name__)

# Per-tool cache: {(server, tool): convention}
_convention_cache: Dict[Tuple[str, str], Optional[str]] = {}


def clear_cache() -> None:
    """Clear the normalization cache (e.g., on config reload)."""
    _convention_cache.clear()
    logger.debug("Parameter normalization cache cleared")


def detect_convention(names: Set[str]) -> Optional[str]:
    """Detect the naming convention from a set of parameter names.

    Examines property names from a JSON Schema and determines whether
    they follow snake_case or camelCase convention. Returns None if
    there are too few names to determine, or if the convention is mixed.

    Args:
        names: Set of parameter/property names from the schema

    Returns:
        'snake_case', 'camelCase', or None if ambiguous
    """
    if len(names) < 2:
        return None

    snake_count = 0
    camel_count = 0

    for name in names:
        if "__" in name:
            continue  # Skip prefixed/metadata names
        if "_" in name:
            snake_count += 1
        elif name[0].islower() and any(c.isupper() for c in name[1:]):
            camel_count += 1

    total = snake_count + camel_count

    # Need at least 2 recognizable names
    if total < 2:
        return None

    # Require 80%+ dominance for a clear signal
    snake_ratio = snake_count / total
    camel_ratio = camel_count / total

    if snake_ratio >= 0.8:
        return "snake_case"
    elif camel_ratio >= 0.8:
        return "camelCase"

    return None  # Mixed — too ambiguous


def to_snake_case(name: str) -> str:
    """Convert a camelCase name to snake_case.

    Args:
        name: Parameter name (e.g., 'thoughtNumber')

    Returns:
        snake_case equivalent (e.g., 'thought_number')
    """
    # Insert underscore before uppercase letters (but not at start)
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    # Handle consecutive uppercase (e.g., 'HTTPServer' -> 'http_server')
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.lower()


def to_camel_case(name: str) -> str:
    """Convert a snake_case name to camelCase.

    Args:
        name: Parameter name (e.g., 'thought_number')

    Returns:
        camelCase equivalent (e.g., 'thoughtNumber')
    """
    components = name.split("_")
    # First component stays lowercase (or as-is), capitalize subsequent
    return components[0] + "".join(x.title() for x in components[1:])


def build_param_map(
    schema_properties: Dict[str, Any], incoming_params: Dict[str, Any]
) -> Dict[str, str]:
    """Build a mapping from normalized parameter names back to schema names.

    Examines the schema's properties and the incoming params, builds a map
    that converts any conventionally-different incoming names to their schema
    equivalent.

    Args:
        schema_properties: The 'properties' dict from a JSON Schema
        incoming_params: The actual arguments passed to the tool

    Returns:
        Dict mapping incoming param name -> schema param name.
        Only includes entries where normalization is needed.
    """
    if not schema_properties or not incoming_params:
        return {}

    schema_names = set(schema_properties.keys())
    incoming_names = set(incoming_params.keys())

    # Names that already match exactly — no normalization needed
    exact_matches = schema_names & incoming_names

    # Names that need normalization
    map_result: Dict[str, str] = {}

    convention = detect_convention(schema_names)
    if convention is None:
        return {}  # Can't determine convention — leave params as-is

    for inc_name in incoming_names - exact_matches:
        if convention == "snake_case":
            # Incoming might be camelCase — convert and check
            candidate = to_snake_case(inc_name)
        else:
            # Incoming might be snake_case — convert and check
            candidate = to_camel_case(inc_name)

        if candidate in schema_names:
            map_result[inc_name] = candidate

    return map_result


def normalize_params(
    server_name: str,
    tool_name: str,
    arguments: Dict[str, Any],
    input_schema: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Normalize parameter names to match the tool's schema.

    Detects the naming convention used by the tool's schema and converts
    incoming params accordingly. Results are cached per (server, tool).

    Args:
        server_name: Server name (for caching)
        tool_name: Tool name (for caching)
        arguments: The raw arguments dict from the caller
        input_schema: The tool's inputSchema dict from the manifest

    Returns:
        Arguments dict with normalized parameter names.
        If no normalization is needed, returns the original dict unchanged.
    """
    if not arguments or input_schema is None:
        return arguments

    cache_key = (server_name, tool_name)
    properties = input_schema.get("properties")

    if not properties:
        return arguments

    # Convention detection is cached per-tool (doesn't vary per call)
    if cache_key not in _convention_cache:
        schema_names = set(properties.keys())
        _convention_cache[cache_key] = detect_convention(schema_names)

    if _convention_cache[cache_key] is None:
        return arguments  # Can't determine convention — leave as-is

    # Mapping depends on which params were passed, compute fresh each call
    mapping = build_param_map(properties, arguments)

    if not mapping:
        return arguments  # No normalization needed

    # Apply the mapping and log
    normalized = dict(arguments)
    for incoming, schema_name in mapping.items():
        if incoming in normalized:
            normalized[schema_name] = normalized.pop(incoming)
            logger.debug(
                f"[PARAM_NORMALIZE] {server_name}__{tool_name}: "
                f"'{incoming}' -> '{schema_name}'"
            )

    return normalized