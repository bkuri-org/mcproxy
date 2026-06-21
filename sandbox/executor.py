"""Sandbox executor for secure code execution.

Features:
- Async execution with Unix Domain Socket IPC
- Pre-execution code validation
- Blocked imports and builtins
- Timeout enforcement
- Memory limits
- Structured error responses
- Synchronous tool execution with immediate results
"""

import ast
import asyncio
import io
import json
import os
import sys
import time
import tokenize
import unicodedata
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from code_validator import validate_code_for_dangerous_patterns
from logging_config import get_logger
from sandbox.access_control import AccessControlConfig, NamespaceAccessControl
from sandbox.code_template import build_wrapped_code
from sandbox.constants import MAX_CODE_SIZE_BYTES
from sandbox.security import BLOCKED_BUILTINS, BLOCKED_IMPORTS
from sandbox.subprocess_runner import run_subprocess, build_env
from sandbox.validation import validate_code

if TYPE_CHECKING:
    from auth import AuthContext, ScopeResolver
    from sandbox.pool import SandboxPool

logger = get_logger(__name__)


class SandboxExecutor:
    """Executes user code securely in a uv subprocess.

    Features:
    - Pre-execution code validation
    - Blocked imports and builtins
    - Timeout enforcement
    - Memory limits
    - Structured error responses
    - Unix Domain Socket IPC for synchronous tool calls
    """

    def __init__(
        self,
        manifest: "AccessControlConfig",
        tool_executor: Callable,
        uv_path: str = "uv",
        default_timeout_secs: int = 60,
        max_concurrency: int = 5,
        pool: Optional["SandboxPool"] = None,
        scope_resolver: Optional["ScopeResolver"] = None,
    ):
        self._manifest = manifest
        self._tool_executor = tool_executor
        self._uv_path = uv_path
        self._default_timeout_secs = default_timeout_secs
        self._max_concurrency = max_concurrency
        self._pool = pool
        self._scope_resolver = scope_resolver

        venv_python = os.path.join(os.path.dirname(sys.executable), "python")
        if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
            self._python_path = venv_python
        else:
            self._python_path = None

    def validate_code(self, code: str) -> tuple[bool, str]:
        """Validate code before execution.

        Performs:
        - Size check
        - Unicode normalization
        - Comment stripping for analysis
        - AST-based dangerous pattern detection
        - AST parsing for blocked imports/builtins

        Args:
            code: Python code to validate

        Returns:
            Tuple of (is_valid: bool, error_message: str)
        """
        code = self._preprocess_js_booleans(code)
        code = self._preprocess_js_object_keys(code)

        if len(code.encode("utf-8")) > MAX_CODE_SIZE_BYTES:
            return False, f"Code exceeds maximum size of {MAX_CODE_SIZE_BYTES} bytes"

        normalized = unicodedata.normalize("NFKC", code)

        code_for_analysis = self._strip_comments(normalized)

        is_safe, danger_error = validate_code_for_dangerous_patterns(code_for_analysis)
        if not is_safe and danger_error:
            return (
                False,
                f"Dangerous pattern detected: {danger_error['error']}. Call get_blocked_functions() for full list.",
            )

        try:
            tree = ast.parse(code_for_analysis)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        blocked = self._check_blocked_imports(tree)
        if blocked:
            return (
                False,
                f"Blocked import detected: {blocked}. Call get_blocked_imports() for full list.",
            )

        blocked_builtin = self._check_blocked_builtins(tree)
        if blocked_builtin:
            return (
                False,
                f"Blocked builtin detected: {blocked_builtin}(). Call get_blocked_functions() for full list.",
            )

        return True, ""

    def _preprocess_js_booleans(self, code: str) -> str:
        """Convert JavaScript-style booleans to Python using AST."""
        js_to_python = {"true": "True", "false": "False", "null": "None"}

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return code

        replacements = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in js_to_python:
                replacements.append(
                    (
                        node.lineno,
                        node.col_offset,
                        node.end_col_offset,
                        js_to_python[node.id],
                    )
                )

        if not replacements:
            return code

        replacements.sort(key=lambda r: (r[0], r[1]), reverse=True)

        lines = code.splitlines(True)

        for lineno, col_start, col_end, replacement in replacements:
            idx = lineno - 1
            line = lines[idx]
            lines[idx] = line[:col_start] + replacement + line[col_end:]

        return "".join(lines)

    def _preprocess_js_object_keys(self, code: str) -> str:
        """Convert JavaScript-style object literals to Python dicts."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return code

        replacements = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key_node in node.keys:
                if isinstance(key_node, ast.Name):
                    replacements.append(
                        (
                            key_node.lineno,
                            key_node.col_offset,
                            key_node.end_col_offset,
                            f'"{key_node.id}"',
                        )
                    )

        if not replacements:
            return code

        replacements.sort(key=lambda r: (r[0], r[1]), reverse=True)

        lines = code.splitlines(True)

        for lineno, col_start, col_end, replacement in replacements:
            idx = lineno - 1
            line = lines[idx]
            lines[idx] = line[:col_start] + replacement + line[col_end:]

        return "".join(lines)

    def _strip_comments(self, code: str) -> str:
        """Remove comments from code for analysis using stdlib tokenize."""
        try:
            tokens = []
            for tok in tokenize.generate_tokens(io.StringIO(code).readline):
                if tok.type != tokenize.COMMENT:
                    tokens.append(tok)
            return tokenize.untokenize(tokens)
        except tokenize.TokenError:
            return code  # ponytail: fallback if tokenize chokes on incomplete code

    def _check_blocked_imports(self, tree: ast.AST) -> Optional[str]:
        """Check for blocked imports in AST."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[0]
                    if module in BLOCKED_IMPORTS:
                        return alias.name

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module = node.module.split(".")[0]
                    if module in BLOCKED_IMPORTS:
                        return node.module

        return None

    def _check_blocked_builtins(self, tree: ast.AST) -> Optional[str]:
        """Check for blocked builtin calls in AST."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in BLOCKED_BUILTINS:
                        return node.func.id

        return None

    async def execute(
        self,
        code: str,
        namespace: str,
        timeout_secs: Optional[int] = None,
        dependencies: Optional[List[str]] = None,
        session: Optional[Any] = None,
        retries: int = 0,
        trace: bool = False,
        auth_context: Optional["AuthContext"] = None,
    ) -> Dict[str, Any]:
        """Execute user code in a uv subprocess with IPC support."""
        timeout = timeout_secs or self._default_timeout_secs

        code = self._preprocess_js_booleans(code)
        code = self._preprocess_js_object_keys(code)

        is_valid, error = validate_code(code)
        if not is_valid:
            return {
                "status": "error",
                "result": None,
                "traceback": f"Validation error: {error}",
                "execution_time_ms": 0,
                "tool_time_ms": 0,
            }

        access_control = NamespaceAccessControl(self._manifest)

        use_pool = (
            self._pool is not None
            and session is None
            and not dependencies
            and not trace
            and auth_context is None
        )

        if use_pool and self._pool is not None:
            manifest_json = json.dumps(
                {
                    "servers": self._manifest.servers,
                    "namespaces": {
                        k: {
                            "servers": v.get("servers", []),
                            "extends": v.get("extends", []),
                        }
                        for k, v in self._manifest.namespaces.items()
                    },
                    "groups": self._manifest.groups,
                }
            )
            result = await self._pool.execute(
                code=code,
                manifest_json=manifest_json,
                namespace=namespace,
                retries=retries,
                max_concurrency=self._max_concurrency,
                timeout=float(timeout),
            )
            response = {
                "status": result.get("status", "error"),
                "result": result.get("result"),
                "traceback": result.get("traceback"),
                "execution_time_ms": result.get("execution_time_ms", 0),
                "tool_time_ms": result.get("tool_time_ms", 0),
            }
            if result.get("stdout"):
                response["stdout"] = result.get("stdout")
            return response

        wrapped_code = self._build_wrapped_code(
            code, namespace, session, retries, trace
        )

        start_time = time.perf_counter()

        try:
            stdout = await self._run_uv_subprocess_async(
                wrapped_code, namespace, access_control, timeout, dependencies or [], auth_context
            )

            execution_time_ms = int((time.perf_counter() - start_time) * 1000)

            try:
                lines = stdout.strip().split("\n")
                json_line = None
                for line in reversed(lines):
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            json.loads(line)
                            json_line = line
                            break
                        except json.JSONDecodeError:
                            continue

                if not json_line:
                    return {
                        "status": "error",
                        "result": None,
                        "traceback": f"No JSON output found. Output: {stdout[:1000]}",
                        "execution_time_ms": execution_time_ms,
                    }

                result = json.loads(json_line)

                if session is not None and "stash_updates" in result:
                    await self._apply_stash_updates_async(
                        session, result["stash_updates"]
                    )

                response_data = {
                    "status": "error" if result.get("traceback") else "success",
                    "result": result.get("result"),
                    "traceback": result.get("traceback"),
                    "execution_time_ms": execution_time_ms,
                    "tool_time_ms": result.get("tool_time_ms", 0),
                }

                if result.get("stdout"):
                    response_data["stdout"] = result.get("stdout")

                if "tool_calls" in result:
                    response_data["tool_calls"] = result["tool_calls"]

                return response_data
            except json.JSONDecodeError as e:
                return {
                    "status": "error",
                    "result": None,
                    "traceback": f"Failed to parse result: {e}\nOutput: {stdout[:1000]}",
                    "execution_time_ms": execution_time_ms,
                    "tool_time_ms": 0,
                }

        except asyncio.TimeoutError:
            execution_time_ms = int((time.perf_counter() - start_time) * 1000)
            return {
                "status": "error",
                "result": None,
                "traceback": (
                    f"Execution timed out after {timeout} seconds.\n\n"
                    f"This timeout includes:\n"
                    f"  - Sandbox startup (~1-2s for uv subprocess)\n"
                    f"  - Tool execution time\n"
                    f"  - Response processing\n\n"
                    f"Under concurrent load, sandbox startup can take longer.\n"
                    f"Suggestions:\n"
                    f"  - Increase timeout_secs (current: {timeout}s)\n"
                    f"  - Reduce concurrent requests\n"
                    f"  - Use action=trace to diagnose where time is spent"
                ),
                "execution_time_ms": execution_time_ms,
                "tool_time_ms": 0,
            }

        except Exception as e:
            execution_time_ms = int((time.perf_counter() - start_time) * 1000)
            logger.exception("Sandbox execution failed")
            return {
                "status": "error",
                "result": None,
                "traceback": str(e),
                "execution_time_ms": execution_time_ms,
                "tool_time_ms": 0,
            }

    async def _apply_stash_updates_async(
        self, session: Any, updates: List[Dict[str, Any]]
    ) -> None:
        """Apply stash updates from sandbox execution to session."""
        for update in updates:
            op = update.get("op")
            key = update.get("key")
            if op == "put":
                value = update.get("value")
                ttl = update.get("ttl_seconds")
                await session.put(key, value, ttl_seconds=ttl)
            elif op == "delete":
                await session.delete(key)
            elif op == "clear":
                await session.clear()

    # Thin delegation methods for test compatibility (tests mock these)

    async def _run_uv_subprocess_async(
        self, code, namespace, access_control, timeout, dependencies, auth_context=None
    ):
        return await run_subprocess(
            code=code, namespace=namespace, timeout=timeout,
            uv_path=self._uv_path, dependencies=dependencies,
            python_path=self._python_path,
            tool_executor=self._tool_executor,
            scope_resolver=self._scope_resolver, auth_context=auth_context,
        )

    def _build_env(self, namespace, access_control, ipc_sock_path=None):
        return build_env(namespace, ipc_sock_path)

    def _wrap_code(self, user_code, namespace, access_control, session=None, retries=0, trace=False):
        return self._build_wrapped_code(user_code, namespace, session, retries, trace)

    def _build_wrapped_code(
        self,
        user_code: str,
        namespace: str,
        session: Optional[Any] = None,
        retries: int = 0,
        trace: bool = False,
    ) -> str:
        """Build wrapped code string using the code template."""
        manifest_json = json.dumps(
            {
                "servers": self._manifest.servers,
                "namespaces": {
                    k: {
                        "servers": v.get("servers", []),
                        "extends": v.get("extends", []),
                    }
                    for k, v in self._manifest.namespaces.items()
                },
                "groups": self._manifest.groups,
            }
        )

        stash_data_json = "{}"
        if session is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    stash_data_json = "{}"
                else:
                    keys = loop.run_until_complete(session.keys())
                    stash_data = {}
                    for key in keys:
                        val = loop.run_until_complete(session.get(key))
                        if val is not None:
                            stash_data[key] = val
                    stash_data_json = json.dumps(stash_data)
            except Exception:
                stash_data_json = "{}"

        return build_wrapped_code(
            user_code=user_code,
            namespace=namespace,
            manifest_json=manifest_json,
            stash_data_json=stash_data_json,
            max_concurrency=self._max_concurrency,
            retries=retries,
            trace=trace,
        )
