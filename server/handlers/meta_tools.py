"""Meta tools registry and tool-call dispatch."""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from .execute import handle_execute_tool
from .help import handle_help_tool
from .inspect import handle_inspect_tool
from .search import handle_search_tool

ToolHandler = Callable[..., Coroutine[Any, Any, Any]]

META_TOOLS: list[dict[str, Any]] = [
    {
        "name": "execute",
        "description": "Execute a command in the sandbox environment and return its output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "search",
        "description": "Search for files and directories matching a pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob or regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in. Defaults to the workspace root.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "inspect",
        "description": "Inspect a file or resource and return its contents or metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file or resource to inspect.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line offset to start from (0-indexed).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "help",
        "description": "List available tools and their descriptions, or get details for a specific tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Optional name of a specific tool to get details for. If omitted, lists all tools.",
                },
            },
        },
    },
]

_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "execute": handle_execute_tool,
    "search": handle_search_tool,
    "inspect": handle_inspect_tool,
    "help": handle_help_tool,
}


async def handle_tools_call(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Dispatch a tool call to the appropriate handler.

    Args:
        tool_name: Name of the tool to invoke (must exist in META_TOOLS).
        arguments: Keyword arguments to pass to the tool handler.

    Returns:
        The result returned by the tool handler.

    Raises:
        ValueError: If *tool_name* is not a recognised meta tool.
    """
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        available = ", ".join(sorted(_TOOL_HANDLERS))
        raise ValueError(
            f"Unknown tool '{tool_name}'. Available tools: {available}"
        )
    return await handler(**arguments)
