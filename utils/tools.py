"""Shared tool validation and normalization.

Error-signaling unification:
Prior to extraction, tool_aggregator.py used log-and-continue with a fallback
sentinel, while manifest/registry.py raised ValueError. This module intentionally
unifies error signaling by strictly raising ValueError (or TypeError) for any
invalid tool. Callers are responsible for catching these exceptions at the call
site to preserve their original error-handling behavior (e.g., log-and-continue).
"""

import copy
import re


def normalize_tool(tool: dict) -> dict:
    """Validates and normalizes a tool definition.

    Enforces the strictest superset of validation rules previously found in
    tool_aggregator.py and manifest/registry.py. Any input rejected by either
    of those modules will be rejected here.

    Contract:
        - Returns a new dict; never mutates the input argument.
        - Raises ValueError or TypeError if the tool is invalid.

    Args:
        tool: A dictionary representing the tool definition.

    Returns:
        A validated, deep-copied, and normalized tool dictionary.

    Raises:
        TypeError: If the input is not a dictionary.
        ValueError: If the tool fails any validation rule.
    """
    if not isinstance(tool, dict):
        raise TypeError(f"Tool must be a dictionary, got {type(tool).__name__}")

    # Deep copy to strictly guarantee immutability of the original argument
    normalized = copy.deepcopy(tool)

    # 1. Validate 'name'
    if "name" not in normalized:
        raise ValueError("Tool validation failed: missing required key 'name'")

    name = normalized["name"]
    if not isinstance(name, str):
        raise ValueError(f"Tool validation failed: 'name' must be a string, got {type(name).__name__}")
    if not name.strip():
        raise ValueError("Tool validation failed: 'name' must be a non-empty string")
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        raise ValueError(
            f"Tool validation failed: 'name' must match ^[a-zA-Z0-9_-]+$, got '{name}'"
        )

    # 2. Validate 'description'
    if "description" not in normalized:
        raise ValueError("Tool validation failed: missing required key 'description'")

    description = normalized["description"]
    if not isinstance(description, str):
        raise ValueError(f"Tool validation failed: 'description' must be a string, got {type(description).__name__}")
    if not description.strip():
        raise ValueError("Tool validation failed: 'description' must be a non-empty string")

    # 3. Validate 'parameters'
    if "parameters" not in normalized:
        raise ValueError("Tool validation failed: missing required key 'parameters'")

    parameters = normalized["parameters"]
    if not isinstance(parameters, dict):
        raise ValueError(f"Tool validation failed: 'parameters' must be a dictionary (JSON Schema), got {type(parameters).__name__}")

    if parameters.get("type") != "object":
        raise ValueError("Tool validation failed: 'parameters.type' must be 'object'")

    # Strictest superset: require 'properties' key in parameters
    if "properties" not in parameters:
        raise ValueError("Tool validation failed: 'parameters' must contain a 'properties' key")

    if not isinstance(parameters["properties"], dict):
        raise ValueError("Tool validation failed: 'parameters.properties' must be a dictionary")

    return normalized
