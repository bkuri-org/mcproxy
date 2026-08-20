"""Tools subpackage - meta-tool definitions and handlers.

This package contains modular handlers for different meta-tool actions:
- router.py: Main handler that routes tools/call to appropriate action
- execute.py: handle_execute(), handle_trace() - code execution handlers
- search.py: handle_search() - tool discovery handler
- inspect.py: handle_inspect() - schema inspection handler
- help.py: handle_help() - documentation handler
"""

from typing import Any, Callable, Dict, List, Optional

from manifest import CapabilityRegistry
from manifest.typescript_gen import generate_compact_instructions

# Re-export router and handlers
from .execute import handle_execute, handle_trace
from .help import handle_help
from .inspect import handle_inspect
from .router import handle_tools_call
from .search import handle_search


# ============================================================================
# META_TOOLS Definition
# ============================================================================

META_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "dispatch",
        "description": "Unified tool: execute (run code), search (find tools), inspect (get schemas), help (get docs), refresh (force tool-catalog rebuild). "
        "Response: {status, result, stdout, traceback}. "
        "CRITICAL SYNTAX RULES for execute code: "
        "1. Tool calls use KEYWORD arguments only: api.server('name').tool_name(param='value') "
        "2. NEVER pass a dict as positional arg: api.server('name').tool_name({'key': 'val'}) will fail "
        "3. If you have a dict, unpack it: api.server('name').tool_name(**my_dict) "
        "4. Tool names are ATTRIBUTES on the server proxy: api.server('wikipedia').search(query='Python') "
        "5. NOT method calls: api.server('wikipedia').tool('search') is WRONG "
        "Example: api.server('wikipedia').search(query='Python'). "
        "Tool names are ATTRIBUTES on the server proxy, not method calls like .tool('name').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["execute", "search", "inspect", "help", "refresh"],
                    "description": "Action to perform: execute (run code), search (find tools), inspect (get tool schemas), help (get documentation)",
                },
                "code": {
                    "type": "string",
                    "description": "Python code (action='execute') or expression (action='inspect'). Execute example: api.server('wikipedia').search(query='Python'). Inspect example: api.server('wikipedia').search",
                },
                "server": {
                    "type": "string",
                    "description": "Server name (action='inspect'). Returns all tool schemas for the server. Use with tool param for a specific tool.",
                },
                "tool": {
                    "type": "string",
                    "description": "Tool name (action='inspect', used with server). Returns schema for a specific tool.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for tools (for action='search'). Returns descriptions only - use inspect for schemas.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional namespace for context",
                },
                "timeout_secs": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (for action='execute')",
                },
                "retries": {
                    "type": "integer",
                    "description": "Number of retries for failed tool calls (for action='execute', default: 0)",
                    "default": 0,
                },

                "topic": {
                    "type": "string",
                    "description": "Help topic (for action='help', e.g., 'sandbox' for security restrictions)",
                },
                "trace": {
                    "type": "boolean",
                    "description": "Enable call tracing (for action='execute', default: false)",
                },
            },
            "required": ["action"],
        },
    },
]


# ============================================================================
# Exports
# ============================================================================

__all__ = [
    "META_TOOLS",
    "handle_tools_call",
    "handle_help",
    "handle_search",
    "handle_execute",
    "handle_trace",
    "handle_inspect",
]
