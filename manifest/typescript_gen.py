"""Generate TypeScript definitions from manifest for LLM type awareness."""

from typing import Any, Dict, Optional


def json_schema_to_ts(schema: Dict[str, Any], indent: int = 0) -> str:
    """Convert JSON Schema to TypeScript type string.

    Args:
        schema: JSON Schema object
        indent: Indentation level

    Returns:
        TypeScript type string
    """
    if not schema:
        return "any"

    schema_type = schema.get("type", "any")

    if schema_type == "string":
        if "enum" in schema:
            return " | ".join(f'"{v}"' for v in schema["enum"])
        return "string"
    elif schema_type == "number" or schema_type == "integer":
        return "number"
    elif schema_type == "boolean":
        return "boolean"
    elif schema_type == "array":
        items = schema.get("items", {})
        item_type = json_schema_to_ts(items, indent)
        return f"{item_type}[]"
    elif schema_type == "object":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        if not properties:
            return "Record<string, any>"

        lines = []
        indent_str = "  " * indent
        for prop_name, prop_schema in properties.items():
            prop_type = json_schema_to_ts(prop_schema, indent + 1)
            optional = "?" if prop_name not in required else ""
            lines.append(f"{indent_str}  {prop_name}{optional}: {prop_type};")

        return "{\n" + "\n".join(lines) + f"\n{indent_str}}}"

    return "any"


def generate_compact_instructions(
    manifest: Dict[str, Any],
    detailed: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate compact instructions with TypeScript-style type hints.

    This is a middle ground: TypeScript syntax but more compact than full definitions.

    Args:
        manifest: Manifest dictionary
        detailed: If True, include parameter signatures for all tools
        config: Optional config dict with search settings (min_words, max_tools)

    Returns:
        Compact instruction string
    """
    tools_by_server = manifest.get("tools_by_server", {})
    servers = manifest.get("servers", {})

    # Get config values with defaults
    search_config = (config or {}).get("search", {})
    min_words = search_config.get("min_words", 2)
    max_tools = search_config.get("max_tools", 5)

    # Format tool limit for display
    max_tools_display = "all" if max_tools <= 0 else f"top {max_tools}"
    min_words_display = "any" if min_words <= 0 else f"{min_words}+"

    lines = [
        "MCProxy v5.0 - Single Tool API",
        "",
        "Tool: mcproxy(action, ...)",
        "  Actions: execute, search, inspect, help",
        "  Response: {status, result, stdout (print output), traceback, execution_time_ms, tool_time_ms, overhead_ms}",
        "",
        "EXECUTE - Run Python code with tool access:",
        "  mcproxy(action='execute', code='...', namespace='...', timeout_secs=60, retries=0)",
        "  - Code is Python. Use api.server('server_name').tool_name(arg1='val1') to call tools.",
        "  - tool_name is the REAL tool name (an attribute, NOT .tool('name')).",
        '  - Use Python dict syntax: {"key": "value"} (NOT {key: "value"}).',
        "  - Results auto-unwrapped (string/list/dict)",
        "",
        "  Examples:",
        '    mcproxy(action="execute", code="api.server(\'wikipedia\').search(query=\'Python\')", namespace="dev")',
        '    mcproxy(action="execute", code="api.server(\'wikipedia\').search(query=\'Python\', limit=5)", namespace="dev")',
        "",
        "SEARCH - Find available servers and tools:",
        "  mcproxy(action='search', query='search terms')            # Find matching tools",
        "  mcproxy(action='search')                                  # List all servers",
        "  mcproxy(action='search', query='...', max_depth=3)        # Include inputSchema + usage examples",
        "  mcproxy(action='search', query='...', max_results=10)     # Control results per server",
        "",
        "INSPECT - Get tool parameter schemas:",
        "  mcproxy(action='inspect', server='server_name', tool='tool_name')   # Specific tool schema",
        "  mcproxy(action='inspect', server='server_name')                     # All tools for server",
        "  mcproxy(action='inspect', code='api.server(\"name\").tool_name')     # Alternative syntax",
        "",
        "Available servers:",
    ]

    # If tools_by_server is empty, fall back to showing server names
    if tools_by_server:
        for server_name, tools in sorted(tools_by_server.items()):
            if detailed:
                lines.append(f"  {server_name}:")
                # Show all tools with parameter signatures
                if tools:
                    if isinstance(tools[0], dict):
                        for tool in tools:
                            tool_name = tool.get("name", "")
                            input_schema = tool.get("inputSchema", {})

                            properties = input_schema.get("properties", {})
                            required = set(input_schema.get("required", []))

                            params = []
                            for prop_name, prop_schema in properties.items():
                                prop_type = json_schema_to_ts(prop_schema, 0)
                                optional = "?" if prop_name not in required else ""
                                # Simplify complex types for display
                                if "|" in prop_type and len(prop_type) > 30:
                                    prop_type = (
                                        prop_type.split("|")[0].strip().strip('"')
                                    )
                                params.append(f"{prop_name}{optional}: {prop_type}")

                            params_str = ", ".join(params) if params else ""
                            lines.append(f"    {tool_name}({params_str})")
                    else:
                        for tool in tools:
                            lines.append(f"    {tool}")
            else:
                # Compact mode: just tool names
                if tools and isinstance(tools[0], dict):
                    tool_names = [t.get("name", "") for t in tools]
                else:
                    tool_names = [str(t) for t in tools]
                lines.append(f"  {server_name}: {', '.join(tool_names)}")
    else:
        # Fallback: show server names with tool counts
        for server_name, server_info in sorted(servers.items()):
            tool_count = server_info.get("tool_count", 0)
            lines.append(f"  {server_name}: {tool_count} tools")

    return "\n".join(lines)
