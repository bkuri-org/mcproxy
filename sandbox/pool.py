"""Sandbox pool for reusing warm sandbox processes.

Reduces per-execution overhead from ~1.5s to ~0.2s by keeping
pre-warmed Python processes ready to execute code.
"""

import asyncio
import json

try:
    import orjson

    _HAS_ORJSON = hasattr(orjson, "loads") and hasattr(orjson, "dumps")
except ImportError:
    _HAS_ORJSON = False

import os
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from logging_config import get_logger
from sandbox.runtime import RUNTIME_CLASSES

logger = get_logger(__name__)

SANDBOX_ENTRYPOINT = """
import sys
import json

{runtime_classes}

# Warmup complete - signal ready
print(json.dumps({{"status": "ready"}}), flush=True)

# Main loop: receive code, execute, return result
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue

    try:
        request = json.loads(line)
    except json.JSONDecodeError as e:
        print(json.dumps({{"status": "error", "error": f"Invalid JSON: {{e}}"}}), flush=True)
        continue

    request_id = request.get("id")
    code = request.get("code")
    manifest_json = request.get("manifest")
    namespace = request.get("namespace")
    retries = request.get("retries", 0)

    if not code:
        print(json.dumps({{"id": request_id, "status": "error", "error": "Missing code"}}), flush=True)
        continue

    # Execute the code
    try:
        # Set up environment
        import io

        _TraceCollector.get().reset()
        _PARALLEL_MAX_CONCURRENCY = request.get("max_concurrency", 5)
        _RETRIES = retries
        _manifest_data = json.loads(manifest_json) if manifest_json else {{}}
        _manifest = _Manifest(_manifest_data)
        _registry = _CapabilityRegistry(_manifest)
        _access_control = _NamespaceAccessControl(_registry)
        api = _APIProxy(namespace, _access_control, _IPCClient(_RETRIES), _manifest)
        stash = _StashProxy({{}})

        # Capture stdout
        _old_stdout = sys.stdout
        sys.stdout = io.StringIO()

        _result = None
        _error = None
        _stdout_output = ""

        try:
            import ast
            import re

            local_vars = {{"__builtins__": __builtins__, "api": api, "stash": stash, "parallel": parallel, "json": json, "re": re, "sys": sys}}

            # Try to extract and evaluate last expression for REPL behavior
            _last_expr_value = None
            try:
                _ast = ast.parse(code)
                if _ast.body:
                    _last_stmt = _ast.body[-1]
                    if isinstance(_last_stmt, ast.Expr):
                        if len(_ast.body) > 1:
                            _setup_code = ast.Module(body=_ast.body[:-1], type_ignores=[])
                            exec(compile(_setup_code, '<string>', 'exec'), local_vars, local_vars)
                        _last_expr_value = eval(compile(ast.Expression(body=_last_stmt.value), '<string>', 'eval'), local_vars, local_vars)
                    else:
                        exec(code, local_vars, local_vars)
                else:
                    exec(code, local_vars, local_vars)
            except (SyntaxError, ValueError):
                exec(code, local_vars, local_vars)

            _stdout_output = sys.stdout.getvalue()
            sys.stdout = _old_stdout

            if _last_expr_value is not None:
                _result = _last_expr_value
            elif "run" in local_vars and callable(local_vars["run"]):
                _result = local_vars["run"]()
            elif "result" in local_vars:
                _result = local_vars["result"]
        except Exception as e:
            import traceback
            _stdout_output = sys.stdout.getvalue()
            sys.stdout = _old_stdout
            _error = traceback.format_exc()

        response = {{
            "id": request_id,
            "status": "error" if _error else "success",
            "result": _result,
            "stdout": _stdout_output,
            "traceback": _error,
            "tool_time_ms": _TraceCollector.get().get_total_tool_time_ms(),
        }}
        print(json.dumps(response), flush=True)

    except Exception as e:
        import traceback
        print(json.dumps({{"id": request_id, "status": "error", "error": traceback.format_exc()}}), flush=True)
"""


class WarmSandbox:
    """A single warm sandbox process."""

    def __init__(self, sandbox_id: int, python_path: str, ipc_sock_path: str, ipc_timeout_secs: float = 60.0):
        self.sandbox_id = sandbox_id
        self.python_path = python_path
        self.ipc_sock_path = ipc_sock_path
        self.ipc_timeout_secs = ipc_timeout_secs
        self.process: Optional[asyncio.subprocess.Process] = None
        self.in_use = False
        self.last_used: float = 0
        self._request_id = 0

    async def start(self) -> bool:
        """Start the sandbox process."""
        try:
            code = SANDBOX_ENTRYPOINT.format(runtime_classes=RUNTIME_CLASSES)

            env = {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "MCPROXY_IPC_SOCK": self.ipc_sock_path,
                "MCPROXY_IPC_TIMEOUT": str(self.ipc_timeout_secs),
            }

            code_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False
            )
            code_file.write(code)
            code_file.close()

            self.process = await asyncio.create_subprocess_exec(
                self.python_path,
                code_file.name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=1024 * 1024,
            )

            # Wait for ready signal
            ready_line = await asyncio.wait_for(
                self.process.stdout.readline(), timeout=30.0
            )
            ready = json.loads(ready_line.decode().strip())

            if ready.get("status") != "ready":
                logger.error(
                    f"[SANDBOX_{self.sandbox_id}] Unexpected ready signal: {ready}"
                )
                return False

            logger.info(f"[SANDBOX_{self.sandbox_id}] Started and ready")
            return True

        except asyncio.TimeoutError:
            logger.error(f"[SANDBOX_{self.sandbox_id}] Startup timeout")
            await self.stop()
            return False
        except Exception as e:
            logger.error(f"[SANDBOX_{self.sandbox_id}] Startup failed: {e}")
            await self.stop()
            return False

    async def stop(self):
        """Stop the sandbox process."""
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except:
                try:
                    self.process.kill()
                    await self.process.wait()
                except:
                    pass
            self.process = None
            logger.info(f"[SANDBOX_{self.sandbox_id}] Stopped")

    async def execute(
        self,
        code: str,
        manifest_json: str,
        namespace: str,
        retries: int,
        max_concurrency: int,
        timeout: float,
        auto_parallel: bool = False,
        max_parallel_calls: int = None,
    ) -> Dict[str, Any]:
        """Execute code in this sandbox."""
        if not self.process or self.process.returncode is not None:
            return {
                "status": "error",
                "result": None,
                "traceback": "Sandbox process not running",
                "execution_time_ms": 0,
            }

        self._request_id += 1
        request = {
            "id": self._request_id,
            "code": code,
            "manifest": manifest_json,
            "namespace": namespace,
            "retries": retries,
            "max_concurrency": max_concurrency,
        }

        start_time = time.perf_counter()

        try:
            # Send request
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            await self.process.stdin.drain()

            # Read response with timeout
            response_line = await asyncio.wait_for(
                self.process.stdout.readline(), timeout=timeout
            )

            execution_time_ms = int((time.perf_counter() - start_time) * 1000)

            response = json.loads(response_line.decode().strip())
            response["execution_time_ms"] = execution_time_ms

            self.last_used = time.time()

            return response

        except asyncio.TimeoutError:
            execution_time_ms = int((time.perf_counter() - start_time) * 1000)
            # Process is likely hung, kill it
            await self.stop()
            return {
                "status": "error",
                "result": None,
                "traceback": f"Execution timed out after {timeout} seconds (sandbox killed)",
                "execution_time_ms": execution_time_ms,
            }
        except Exception as e:
            execution_time_ms = int((time.perf_counter() - start_time) * 1000)
            logger.error(f"[SANDBOX_{self.sandbox_id}] Execute failed: {e}")
            await self.stop()
            return {
                "status": "error",
                "result": None,
                "traceback": str(e),
                "execution_time_ms": execution_time_ms,
            }

    def is_healthy(self) -> bool:
        """Check if sandbox is healthy and available."""
        return (
            self.process is not None
            and self.process.returncode is None
            and not self.in_use
        )


class SandboxPool:
    """Pool of warm sandbox processes for fast execution.

    Architecture:
    - ONE long-lived IPC server handles tool calls from all sandboxes
    - Multiple warm sandbox processes connect to it
    - Each tool call creates a new connection (handled by _IPCClient)
    - The IPC server routes tool calls to tool_executor
    """

    def __init__(
        self,
        tool_executor: Callable,
        uv_path: str = "uv",
        pool_size: int = 3,
        max_pool_size: int = 10,
        idle_timeout_secs: float = 300.0,
        ipc_timeout_secs: float = 60.0,
    ):
        self._tool_executor = tool_executor
        self.uv_path = uv_path
        self.pool_size = pool_size
        self.max_pool_size = max_pool_size
        self.idle_timeout_secs = idle_timeout_secs
        self.ipc_timeout_secs = ipc_timeout_secs

        venv_python = os.path.join(os.path.dirname(sys.executable), "python")
        if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
            self.python_path = venv_python
            logger.info(f"[POOL] Using venv Python: {self.python_path}")
        else:
            self.python_path = uv_path
            logger.info(f"[POOL] Using uv fallback: {self.python_path}")

        self._sandboxes: List[WarmSandbox] = []
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_pool_size)
        self._cleanup_task: Optional[asyncio.Task] = None

        self._ipc_temp_dir: Optional[str] = None
        self._ipc_sock_path: Optional[str] = None
        self._ipc_server: Optional[asyncio.Server] = None

    async def start(self):
        """Start the pool with initial warm sandboxes."""
        # Clean up stale IPC socket directories from previous runs
        import glob

        for stale_dir in glob.glob("/tmp/mcproxy_pool_ipc_*"):
            try:
                shutil.rmtree(stale_dir)
            except OSError:
                pass

        self._ipc_temp_dir = tempfile.mkdtemp(prefix="mcproxy_pool_ipc_")
        self._ipc_sock_path = os.path.join(self._ipc_temp_dir, "ipc.sock")

        self._ipc_server = await asyncio.start_unix_server(
            self._handle_ipc_connection,
            path=self._ipc_sock_path,
        )
        os.chmod(self._ipc_sock_path, 0o600)
        logger.info(f"[POOL] IPC server listening on {self._ipc_sock_path}")

        logger.info(f"[POOL] Starting with {self.pool_size} warm sandboxes")

        for i in range(self.pool_size):
            sandbox = await self._create_sandbox()
            if sandbox:
                self._sandboxes.append(sandbox)

        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info(f"[POOL] Started with {len(self._sandboxes)} sandboxes ready")

    async def stop(self):
        """Stop all sandbox processes and IPC server."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        for sandbox in self._sandboxes:
            await sandbox.stop()

        self._sandboxes.clear()

        if self._ipc_server is not None:
            self._ipc_server.close()
            await self._ipc_server.wait_closed()
            self._ipc_server = None

        if self._ipc_sock_path and os.path.exists(self._ipc_sock_path):
            try:
                os.unlink(self._ipc_sock_path)
            except OSError:
                pass

        if self._ipc_temp_dir and os.path.exists(self._ipc_temp_dir):
            try:
                shutil.rmtree(self._ipc_temp_dir)
            except OSError:
                pass

        self._ipc_sock_path = None
        self._ipc_temp_dir = None

        logger.info("[POOL] Stopped")

    async def _create_sandbox(self) -> Optional[WarmSandbox]:
        """Create and start a new sandbox."""
        self._next_id += 1
        sandbox = WarmSandbox(self._next_id, self.python_path, self._ipc_sock_path, self.ipc_timeout_secs)

        if await sandbox.start():
            return sandbox

        return None

    async def _handle_ipc_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle incoming IPC connection from sandbox subprocess.

        Args:
            reader: Stream reader for incoming data
            writer: Stream writer for outgoing data
        """
        try:
            data = await reader.read(65536)
            if not data:
                logger.debug("[POOL_IPC] Empty data received, client disconnected")
                return

            try:
                request = json.loads(data)
            except json.JSONDecodeError as e:
                response = {
                    "call_id": None,
                    "status": "error",
                    "error": f"Invalid JSON: {e}",
                }
                writer.write(json.dumps(response).encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            call_id = request.get("call_id")
            server = request.get("server")
            tool = request.get("tool")
            args = request.get("args", {})
            call_start = time.perf_counter()
            logger.info(f"[POOL_IPC] server={server} tool={tool} args={args}")

            try:
                result = self._tool_executor(server, tool, args)
                if asyncio.iscoroutine(result):
                    result = await result

                call_ms = int((time.perf_counter() - call_start) * 1000)
                logger.info(
                    f"[POOL_IPC_COMPLETE] server={server} tool={tool} duration_ms={call_ms}"
                )

                response = {
                    "call_id": call_id,
                    "status": "success",
                    "result": result,
                    "duration_ms": call_ms,
                }
            except Exception as e:
                call_ms = int((time.perf_counter() - call_start) * 1000)
                error_msg = str(e)
                logger.error(f"[POOL_IPC] Tool call failed: {server}.{tool}: {e}")

                if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    error_msg = (
                        f"Upstream MCP server '{server}' timed out. "
                        f"The tool '{tool}' did not respond within the configured timeout. "
                        f"This is a server-side issue, not a mcproxy issue. "
                        f"Original error: {error_msg}"
                    )

                response = {
                    "call_id": call_id,
                    "status": "error",
                    "error": error_msg,
                    "duration_ms": call_ms,
                }

            try:
                if _HAS_ORJSON:
                    response_bytes = orjson.dumps(response)
                else:
                    response_bytes = json.dumps(response).encode()
            except Exception as serialize_err:
                logger.error(
                    f"[POOL_IPC] Failed to serialize response: {serialize_err}"
                )
                try:
                    response_bytes = json.dumps(response).encode()
                except Exception as json_err:
                    logger.error(
                        f"[POOL_IPC] Failed to serialize response with json: {json_err}"
                    )
                    response_bytes = json.dumps(
                        {
                            "call_id": call_id,
                            "status": "error",
                            "error": f"Response serialization failed: {serialize_err}",
                        }
                    ).encode()

            if response_bytes:
                try:
                    writer.write(response_bytes)
                    await writer.drain()
                    logger.debug(f"[POOL_IPC] Response sent successfully")
                except Exception as write_err:
                    logger.error(f"[POOL_IPC] Failed to write response: {write_err}")
            else:
                logger.error("[POOL_IPC] response_bytes is empty, cannot send")

        except Exception as e:
            logger.error(f"[POOL_IPC] Connection error: {e}")
            error_response = {
                "call_id": None,
                "status": "error",
                "error": f"IPC connection error: {e}",
            }
            try:
                writer.write(json.dumps(error_response).encode())
                await writer.drain()
            except Exception:
                logger.error("[POOL_IPC] Failed to send error response")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def execute(
        self,
        code: str,
        manifest_json: str,
        namespace: str,
        retries: int = 0,
        max_concurrency: int = 5,
        timeout: float = 60.0,
        auto_parallel: bool = False,
        max_parallel_calls: int = None,
    ) -> Dict[str, Any]:
        """Execute code using a sandbox from the pool."""
        async with self._semaphore:
            sandbox = None

            async with self._lock:
                available = [s for s in self._sandboxes if s.is_healthy()]

                if available:
                    sandbox = available[0]
                    sandbox.in_use = True

            if sandbox is None:
                async with self._lock:
                    if len(self._sandboxes) < self.max_pool_size:
                        pass

                logger.info(
                    f"[POOL] Creating new sandbox (pool exhausted, {len(self._sandboxes)}/{self.max_pool_size})"
                )
                new_sandbox = await self._create_sandbox()

                async with self._lock:
                    if new_sandbox:
                        self._sandboxes.append(new_sandbox)
                        sandbox = new_sandbox
                        sandbox.in_use = True

            if sandbox is None:
                async with self._lock:
                    available = [s for s in self._sandboxes if s.is_healthy()]
                    if available:
                        sandbox = available[0]
                        sandbox.in_use = True

            if sandbox is None:
                return {
                    "status": "error",
                    "result": None,
                    "traceback": "No available sandboxes in pool",
                    "execution_time_ms": 0,
                }

            try:
                result = await sandbox.execute(
                    code, manifest_json, namespace, retries, max_concurrency, timeout
                )
                return result
            finally:
                sandbox.in_use = False

                if not sandbox.is_healthy():
                    async with self._lock:
                        if sandbox in self._sandboxes:
                            self._sandboxes.remove(sandbox)
                            logger.info(
                                f"[POOL] Removed dead sandbox {sandbox.sandbox_id}"
                            )

    async def _cleanup_loop(self):
        """Periodically clean up idle sandboxes."""
        while True:
            try:
                await asyncio.sleep(60.0)  # Check every minute

                async with self._lock:
                    now = time.time()
                    to_remove = []

                    for sandbox in self._sandboxes:
                        # Keep minimum pool size
                        if len(self._sandboxes) - len(to_remove) <= self.pool_size:
                            break

                        # Remove idle sandboxes
                        if (
                            not sandbox.in_use
                            and sandbox.last_used > 0
                            and now - sandbox.last_used > self.idle_timeout_secs
                        ):
                            to_remove.append(sandbox)

                    for sandbox in to_remove:
                        await sandbox.stop()
                        self._sandboxes.remove(sandbox)
                        logger.info(f"[POOL] Removed idle sandbox {sandbox.sandbox_id}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[POOL] Cleanup error: {e}")

    def stats(self) -> Dict[str, Any]:
        """Get pool statistics."""
        healthy = sum(1 for s in self._sandboxes if s.is_healthy())
        in_use = sum(1 for s in self._sandboxes if s.in_use)

        return {
            "total": len(self._sandboxes),
            "healthy": healthy,
            "in_use": in_use,
            "pool_size": self.pool_size,
            "max_pool_size": self.max_pool_size,
        }
