"""Code wrapper template for sandbox execution.

Builds the Python code string that wraps user code with sandbox
infrastructure (api proxy, stash, parallel, error hints).
"""

from sandbox.runtime import RUNTIME_CLASSES

# ponytail: single string template, function below formats it


def build_wrapped_code(
    user_code: str,
    namespace: str,
    manifest_json: str,
    stash_data_json: str,
    max_concurrency: int,
    retries: int,
    trace: bool,
) -> str:
    """Build the wrapped code string for sandbox execution.

    Args:
        user_code: User's Python code
        namespace: Namespace for access control
        manifest_json: JSON-serialized manifest data
        stash_data_json: JSON-serialized initial stash data
        max_concurrency: Max parallel execution concurrency
        retries: Number of retries for failed tool calls
        trace: Enable call tracing

    Returns:
        Complete Python code string ready for subprocess execution
    """
    return f'''
import json
import sys
import io
import ast

{RUNTIME_CLASSES}

def get_blocked_functions():
    """Return list of functions blocked in sandbox for security."""
    return [
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
    ]

def get_blocked_imports():
    """Return list of modules blocked from import."""
    return [
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
    ]

def get_blocked_attributes():
    """Return list of blocked dunder attributes."""
    return [
        "__class__",
        "__bases__",
        "__subclasses__",
        "__globals__",
        "__locals__",
        "__code__",
        "__builtins__",
        "__dict__",
        "__mro__",
        "__init__",
        "__new__",
        "__reduce__",
        "__getstate__",
        "__setstate__",
    ]

_PARALLEL_MAX_CONCURRENCY = {max_concurrency}
_RETRIES = {retries}
_TRACE_ENABLED = {trace}
_ipc_client = _IPCClient(_RETRIES)
_manifest_data = json.loads({repr(manifest_json)})
_manifest = _Manifest(_manifest_data)
_registry = _CapabilityRegistry(_manifest)
_access_control = _NamespaceAccessControl(_registry)
api = _APIProxy("{namespace}", _access_control, _ipc_client, _manifest)
_stash_initial = json.loads({repr(stash_data_json)})
stash = _StashProxy(_stash_initial)

# Enable tracing if requested
if _TRACE_ENABLED:
    _TraceCollector.get().enable()

_result = None
_error = None
_stdout_output = ""

try:
    import re

    # Capture stdout
    _old_stdout = sys.stdout
    sys.stdout = io.StringIO()

    local_vars = {{"__builtins__": __builtins__, "api": api, "stash": stash, "parallel": parallel, "json": json, "re": re, "sys": sys, "get_blocked_functions": get_blocked_functions, "get_blocked_imports": get_blocked_imports, "get_blocked_attributes": get_blocked_attributes}}

    # Try to extract and evaluate last expression for REPL behavior
    _last_expr_value = None
    try:
        _ast = ast.parse({repr(user_code)})
        if _ast.body:
            _last_stmt = _ast.body[-1]
            # If last statement is an expression, capture its value
            if isinstance(_last_stmt, ast.Expr):
                # Execute all but the last statement
                if len(_ast.body) > 1:
                    _setup_code = ast.Module(body=_ast.body[:-1], type_ignores=[])
                    exec(compile(_setup_code, '<string>', 'exec'), local_vars, local_vars)
                # Evaluate the last expression and capture result
                _last_expr_value = eval(compile(ast.Expression(body=_last_stmt.value), '<string>', 'eval'), local_vars, local_vars)
            else:
                # Last statement is not an expression, execute all
                exec({repr(user_code)}, local_vars, local_vars)
        else:
            exec({repr(user_code)}, local_vars, local_vars)
    except (SyntaxError, ValueError):
        # Fallback to simple exec if AST parsing fails
        exec({repr(user_code)}, local_vars, local_vars)

    # Restore stdout and capture output
    _stdout_output = sys.stdout.getvalue()
    sys.stdout = _old_stdout

    # Determine result: last expression > result variable > run() function
    if _last_expr_value is not None:
        _result = _last_expr_value
    elif "run" in local_vars and callable(local_vars["run"]):
        run_func = local_vars["run"]
        _result = run_func()
    elif "result" in local_vars:
        _result = local_vars["result"]
except NameError as e:
    import traceback
    _stdout_output = sys.stdout.getvalue()
    sys.stdout = _old_stdout
    _error = traceback.format_exc()

    # Check for common mistakes
    error_str = str(_error)

    # Pattern: blocked builtin access
    blocked_names = ["eval", "exec", "compile", "open", "input", "__import__", "breakpoint", "hasattr", "getattr", "setattr", "delattr"]
    found_blocked = False
    for _bn in blocked_names:
        if f"name '{{_bn}}'" in error_str.lower() or f"name '{{_bn}}'" in error_str:
            _error = f"""NameError: '{{_bn}}' is blocked for security.

Call get_blocked_functions() to see all blocked functions."""
            found_blocked = True
            break

    if not found_blocked:
        # Pattern 1: server__tool() direct call
        match = re.search(r"name '([\\w]+__[\\w]+)' is not defined", error_str)
        if match:
            tool_name = match.group(1)
            parts = tool_name.split("__", 1)
            if len(parts) == 2:
                server, tool = parts
                _error = f"""NameError: '{{tool_name}}' is not a function.

Use api.server() to call tools:

    result = api.server("{{server}}").{{tool}}(...)

Available: api.manifest()"""
        elif "call_tool" in error_str and "is not defined" in error_str:
            # Pattern 2: call_tool without api prefix
            _error = """NameError: 'call_tool' is not defined.

Use api.call_tool():

    result = api.call_tool("server", "tool", {{"arg": "value"}})"""
        elif re.search(r"name '(server|manifest)' is not defined", error_str):
            # Pattern 3: Using 'server' directly
            _error = """NameError: Use the 'api' object to access tools.

    result = api.server("name").tool(args)

api.manifest()"""
except Exception as e:
    import traceback
    _stdout_output = sys.stdout.getvalue()
    sys.stdout = _old_stdout
    _error = traceback.format_exc()

output = {{
    "result": _result,
    "stdout": _stdout_output,
    "traceback": _error,
    "stash_updates": stash._get_updates(),
}}

# Include trace data if tracing was enabled
if _TRACE_ENABLED:
    output["tool_calls"] = _TraceCollector.get().get_calls()

print(json.dumps(output))
'''
