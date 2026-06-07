"""Generate usage examples from tool input schemas.

Creates concise Python call examples from JSON Schema definitions.
Used by Enhanced Search to show agents how to call tools correctly.
"""

from typing import Any, Dict, List, Optional

# Param names to skip in examples (too generic or server-injected)
_SKIP_PARAMS: frozenset = frozenset(
    [
        "context",
        "request_context",
        "metadata",
        "trace_id",
        "credentials",
        "api_key",
        "auth_token",
    ]
)

# Placeholder templates for common param names
_PLACEHOLDER_TEMPLATES: Dict[str, str] = {
    "query": "<query>",
    "search": "<query>",
    "q": "<query>",
    "id": "<id>",
    "name": "<name>",
    "title": "<title>",
    "url": "<url>",
    "path": "<path>",
    "file": "<file>",
    "code": "<code>",
    "text": "<text>",
    "message": "<message>",
    "content": "<content>",
    "description": "<description>",
    "email": "<email>",
    "username": "<username>",
    "password": "<password>",
    "limit": 5,
    "max_results": 5,
    "count": 3,
    "max_count": 3,
    "max_tokens": 100,
    "temperature": 0.7,
    "timeout": 30,
    "port": 8080,
    "enabled": True,
    "verbose": False,
    "brief": False,
}

# Maximum example length before truncation
_MAX_EXAMPLE_LENGTH = 120


def generate_tool_example(
    server_name: str,
    tool_name: str,
    input_schema: Optional[Dict[str, Any]],
) -> str:
    """Generate a concise Python call example for a tool.

    Produces something like:
        api.server('wikipedia').search(query='<query>', limit=5)

    Args:
        server_name: The server name
        tool_name: The raw tool name (may already be prefixed)
        input_schema: JSON Schema object for the tool's parameters

    Returns:
        A Python code string showing how to call this tool
    """
    if input_schema is None or not isinstance(input_schema, dict):
        return f"api.server('{server_name}').{tool_name}(...)"

    properties = input_schema.get("properties")
    required_params: List[str] = input_schema.get("required", [])

    if not properties:
        return f"api.server('{server_name}').{tool_name}()"

    # Collect example argument strings
    args: List[str] = []

    # Required params first (in schema order)
    for param_name in properties:
        if param_name in _SKIP_PARAMS:
            continue
        if param_name in required_params:
            example = _param_example(param_name, properties[param_name])
            args.append(f"{param_name}={example}")

    # Add at most one optional param as a hint
    if not required_params and len(properties) > 0:
        # No required params — show the first meaningful param
        for param_name in properties:
            if param_name in _SKIP_PARAMS:
                continue
            example = _param_example(param_name, properties[param_name])
            args.append(f"{param_name}={example}")
            break

    # Add a ... hint if there are more params
    if len(args) < len(properties) and len(properties) > 1:
        remaining = len(properties) - len(args)
        if remaining > 0:
            args.append("...")

    args_str = ", ".join(args)

    # Truncate if too long
    if len(args_str) > _MAX_EXAMPLE_LENGTH:
        args_str = args_str[:_MAX_EXAMPLE_LENGTH].rstrip(", ") + "..."

    return f"api.server('{server_name}').{tool_name}({args_str})"


def _param_example(param_name: str, param_schema: Dict[str, Any]) -> str:
    """Generate an example value string for a parameter.

    Args:
        param_name: The parameter name
        param_schema: The JSON Schema for this parameter

    Returns:
        A Python-code-ready representation of an example value
    """
    # Check for explicit example in schema
    schema_example = param_schema.get("example")
    if schema_example is not None:
        return _format_value(schema_example)

    schema_type = param_schema.get("type", "string")

    # Check if param has a known placeholder template
    name_lower = param_name.lower()
    if name_lower in _PLACEHOLDER_TEMPLATES:
        val = _PLACEHOLDER_TEMPLATES[name_lower]
        if isinstance(val, str):
            # String placeholder — quote it for Python code if param expects string
            if schema_type == "string":
                return f"'{val}'"
            return val
        return _format_value(val)

    # Use enum first value as hint if available
    enum_values = param_schema.get("enum")
    if enum_values and len(enum_values) > 0:
        return _format_value(enum_values[0])

    # Check for boolean type
    if schema_type == "boolean":
        return "true" if param_schema.get("default", True) else "false"

    # Check for number type
    if schema_type in ("number", "integer"):
        default = param_schema.get("default", 0)
        return str(default) if default != 0 else "<number>"

    # Check for array type
    if schema_type == "array":
        return "[<items>]"

    # Check for object type
    if schema_type == "object":
        return "{<key>: <value>}"

    # Default string type or unknown: use param name as quoted hint
    return f"'<{param_name}>'"


def _format_value(value: Any) -> str:
    """Format a value for display in a code example.

    Args:
        value: The value to format

    Returns:
        Formatted string representation
    """
    if isinstance(value, str):
        return f"'{value}'"
    elif isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, list):
        return str(value)
    elif isinstance(value, dict):
        return str(value)
    return str(value)