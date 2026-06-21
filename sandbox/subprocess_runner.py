"""Subprocess runner for sandbox code execution.

Handles uv subprocess spawning, IPC socket lifecycle, and tool call
dispatch from the sandbox subprocess back to MCP servers.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import orjson

from logging_config import get_logger

if TYPE_CHECKING:
    from auth import AuthContext, ScopeResolver

logger = get_logger(__name__)


def build_env(
    namespace: str,
    ipc_sock_path: Optional[str] = None,
) -> Dict[str, str]:
    """Build clean environment for subprocess."""
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "SANDBOX_NAMESPACE": namespace,
    }
    if ipc_sock_path:
        env["MCPROXY_IPC_SOCK"] = ipc_sock_path
    return env


async def run_subprocess(
    code: str,
    namespace: str,
    timeout: int,
    uv_path: str,
    dependencies: List[str],
    python_path: Optional[str],
    tool_executor: Callable,
    scope_resolver: Optional["ScopeResolver"],
    auth_context: Optional["AuthContext"],
) -> str:
    """Run code in uv subprocess with IPC support.

    Args:
        code: Python code to execute
        namespace: Namespace for access control
        timeout: Timeout in seconds
        uv_path: Path to uv binary
        dependencies: List of pip dependencies
        python_path: Path to venv python (if available)
        tool_executor: Callable to execute MCP tools
        scope_resolver: Optional ScopeResolver for credential injection
        auth_context: Optional AuthContext for credential injection

    Returns:
        stdout from subprocess

    Raises:
        asyncio.TimeoutError: If timeout exceeded
        RuntimeError: If process fails
    """
    ipc_temp_dir = tempfile.mkdtemp(prefix="mcproxy_ipc_")
    ipc_sock_path = os.path.join(ipc_temp_dir, "ipc.sock")
    ipc_server: Optional[asyncio.Server] = None

    async def handle_ipc(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await handle_ipc_connection(
            reader, writer, tool_executor, scope_resolver, auth_context
        )

    try:
        ipc_server = await asyncio.start_unix_server(
            handle_ipc, path=ipc_sock_path
        )
        os.chmod(ipc_sock_path, 0o600)

        env = build_env(namespace, ipc_sock_path)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as code_file:
            code_file.write(code)
            code_file_path = code_file.name

        try:
            if python_path and os.path.isfile(python_path) and os.access(python_path, os.X_OK):
                cmd = [python_path, code_file_path]
                logger.debug(f"Running venv subprocess: {python_path}")
            else:
                cmd = [uv_path, "run"]
                for dep in dependencies:
                    cmd.extend(["--with", dep])
                cmd.extend(["python", code_file_path])
                logger.debug(f"Running uv subprocess: {' '.join(cmd[:5])}...")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if process.returncode != 0:
                error_msg = stderr or f"Process exited with code {process.returncode}"
                raise RuntimeError(error_msg)

            return stdout
        finally:
            try:
                os.unlink(code_file_path)
            except OSError:
                pass
    finally:
        if ipc_server is not None:
            ipc_server.close()
            await ipc_server.wait_closed()

        for path in (ipc_sock_path, ipc_temp_dir):
            if path and os.path.exists(path):
                try:
                    (shutil.rmtree if os.path.isdir(path) else os.unlink)(path)
                except OSError:
                    pass


async def handle_ipc_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    tool_executor: Callable,
    scope_resolver: Optional["ScopeResolver"],
    auth_context: Optional["AuthContext"],
) -> None:
    """Handle incoming IPC connection from sandbox subprocess."""
    try:
        data = await reader.read(65536)
        if not data:
            logger.debug("[IPC] Empty data received, client disconnected")
            return

        try:
            request = orjson.loads(data)
        except (orjson.JSONDecodeError, json.JSONDecodeError) as e:
            response = {
                "call_id": None,
                "status": "error",
                "error": f"Invalid JSON: {e}",
            }
            writer.write(orjson.dumps(response))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        call_id = request.get("call_id")
        server = request.get("server")
        tool = request.get("tool")
        args = request.get("args", {})
        call_start = time.perf_counter()
        logger.info(
            f"[IPC_EXEC] server={server} tool={tool} args={args} type={type(args)}"
        )

        if args is None:
            args = {}

        injected_args = dict(args)

        if scope_resolver is not None and auth_context is not None:
            fq_tool_name = f"{server}.{tool}"
            try:
                resolved = scope_resolver.resolve_for_tool(
                    fq_tool_name, auth_context.scopes
                )
                if resolved is not None:
                    if resolved.inject_type == "env":
                        env_key = f"_env_{resolved.inject_as}"
                        injected_args[env_key] = resolved.value
                    elif resolved.inject_type == "header":
                        header_key = f"_header_{resolved.inject_as}"
                        injected_args[header_key] = resolved.value
                    logger.debug(
                        f"[IPC_CREDENTIAL] Injected {resolved.inject_type} "
                        f"'{resolved.inject_as}' for tool {fq_tool_name}"
                    )
            except Exception as cred_error:
                call_ms = int((time.perf_counter() - call_start) * 1000)
                error_msg = str(cred_error)
                logger.error(f"[IPC_CREDENTIAL_ERROR] {error_msg}")
                response = {
                    "call_id": call_id,
                    "status": "error",
                    "error": error_msg,
                    "duration_ms": call_ms,
                }
                writer.write(orjson.dumps(response))
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

        try:
            result = tool_executor(server, tool, injected_args)
            if asyncio.iscoroutine(result):
                result = await result

            call_ms = int((time.perf_counter() - call_start) * 1000)
            logger.info(
                f"[IPC_EXEC_COMPLETE] server={server} tool={tool} duration_ms={call_ms}"
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
            logger.error(f"[IPC] Tool call failed: {server}.{tool}: {e}")

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
            response_bytes = orjson.dumps(response)
        except Exception as serialize_err:
            logger.error(f"[IPC] Failed to serialize response: {serialize_err}")
            response_bytes = orjson.dumps(
                {
                    "call_id": call_id,
                    "status": "error",
                    "error": f"Response serialization failed: {serialize_err}",
                }
            )
        writer.write(response_bytes)
        await writer.drain()

    except Exception as e:
        logger.error(f"[IPC] Connection error: {e}")
        error_response = {
            "call_id": None,
            "status": "error",
            "error": f"IPC connection error: {e}",
        }
        try:
            writer.write(orjson.dumps(error_response))
            await writer.drain()
        except Exception:
            logger.error("[IPC] Failed to send error response")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
