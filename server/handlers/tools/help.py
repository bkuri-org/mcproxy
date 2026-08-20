"""Meta-tool help handler."""

import json
from typing import Any, Dict


def handle_help(msg_id: Any, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Handle help action - return documentation about available actions.

    Args:
        msg_id: JSON-RPC message ID
        arguments: Help arguments (optional: topic)

    Returns:
        MCP response with help documentation
    """
    topic = arguments.get("topic", "").lower()

    if topic == "sandbox":
        help_data = {
            "topic": "sandbox",
            "description": "Security sandbox restrictions and blocked functions",
            "blocked_functions": [
                "eval()",
                "exec()",
                "compile()",
                "open() (file operations)",
                "input()",
                "__import__()",
                "breakpoint()",
                "hasattr()",
                "getattr()",
                "setattr()",
                "delattr()",
                "os.system()",
                "os.popen()",
                "subprocess.* (all subprocess calls)",
                "pickle.loads() / pickle.load()",
                "marshal.loads() / marshal.load()",
                "importlib.import_module()",
            ],
            "blocked_imports": [
                "os",
                "sys",
                "subprocess",
                "socket",
                "http",
                "urllib",
                "requests",
                "shutil",
                "tempfile",
                "multiprocessing",
                "pickle",
                "marshal",
                "importlib",
                "builtins",
            ],
            "how_to_check": {
                "blocked_functions": "get_blocked_functions()",
                "blocked_imports": "get_blocked_imports()",
                "blocked_attributes": "get_blocked_attributes()",
            },
            "examples": [
                {
                    "description": "Check blocked functions",
                    "code": "get_blocked_functions()",
                },
                {
                    "description": "Check blocked imports",
                    "code": "get_blocked_imports()",
                },
            ],
        }
    else:
        help_data = {
            "actions": {
                "execute": {
                    "description": "Run Python code with access to MCP tools",
                    "syntax": "api.server('server_name').actual_tool_name(param1='val1', param2='val2')",
                    "important": "Tool names are ATTRIBUTES on the server proxy. Use api.server('wikipedia').search(query='x'), NOT api.server('wikipedia').tool('search')(query='x').",
                    "python_syntax": 'Use Python dict syntax: {"key": "value"}. JavaScript-style {key: "value"} is NOT valid Python.',
                    "usage": "mcproxy(action='execute', code='...', namespace='...', timeout_secs=60, retries=0)",
                    "available_objects": {
                        "api": "Access MCP servers: api.server('name').tool_name(args)",
                        "parallel": "Execute multiple tool calls concurrently: parallel([lambda: ...])",
                    },
                    "parameters": {
                        "timeout_secs": "Optional execution timeout (default: 60 seconds)",
                        "retries": "Number of retries for failed tool calls (default: 0). Only retries on timeout/network errors with exponential backoff.",
                    },
                },
                "search": {
                    "description": "Discover available servers and tools (no schemas - use inspect for schemas)",
                    "usage": "mcproxy(action='search', query='...', namespace='...')",
                },
                "inspect": {
                    "description": "Get detailed schemas for a server's tools (parameters, types, required fields)",
                    "usage": "mcproxy(action='inspect', server='name', tool='tool_name')",
                    "note": "Pass server and tool as separate parameters. Example: mcproxy(action='inspect', server='wikipedia', tool='search')",
                },
                "help": {
                    "description": "Get help and documentation",
                    "usage": "mcproxy(action='help') or mcproxy(action='help', topic='sandbox')",
                    "available_topics": ["sandbox"],
                },
            },
            "examples": [
                {
                    "description": "Discover available servers",
                    "code": "mcproxy(action='search', query='', namespace='default')",
                },
                {
                    "description": "Call a tool on a server",
                    "code": "mcproxy(action='execute', code='api.server(\"wikipedia\").search(query=\"Python\")', namespace='default')",
                },
                {
                    "description": "Inspect tool schemas",
                    "code": "mcproxy(action='inspect', server='wikipedia', tool='search', namespace='default')",
                },
                {
                    "description": "Inspect all tools for a server",
                    "code": "mcproxy(action='inspect', server='wikipedia', namespace='default')",
                },
                {
                    "description": "Get sandbox restrictions",
                    "code": "mcproxy(action='help', topic='sandbox')",
                },
                {
                    "description": "Run parallel tool calls (faster than sequential)",
                    "code": 'mcproxy(action=\'execute\', code=\'results = parallel([\\n    lambda: api.server("wikipedia").search(query="Python"),\\n    lambda: api.server("wikipedia").search(query="JavaScript")\\n])\', namespace=\'default\')',
                },
            ],
            "quick_start": {
                "step_1_discover": "mcproxy(action='search', query='', namespace='default')",
                "step_2_inspect": "mcproxy(action='inspect', server='wikipedia', tool='search')",
                "step_3_execute": "mcproxy(action='execute', code='api.server(\"wikipedia\").search(query=\"Python\")', namespace='default')",
            },
            "tips": {
                "tool_names_are_attributes": "Use api.server('name').actual_tool_name(args) - tool_name is an attribute, not a method call like .tool('name').",
                "python_dicts": 'Use {"key": "value"} not {key: "value"}. Python requires quoted dict keys.',
                "parallel_execution": "Use parallel() for multiple tool calls - much faster than sequential",
                "timeout_errors": "If you see timeout errors, they're from upstream MCP servers (not mcproxy). Try increasing timeout_secs or use retries=3 for automatic retry.",
                "server_discovery": "Use search(query='') to list all available servers and their tool counts",
                "automatic_retries": "Use retries=N to automatically retry failed tool calls. Only retries on timeout/network errors with exponential backoff (100ms, 200ms, 400ms, ...).",
            },
        }

    content = [{"type": "text", "text": json.dumps(help_data)}]
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}
