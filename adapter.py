"""Built-in stdio-to-HTTP bridge for MCP servers.

Launches a stdio MCP server as a subprocess and exposes it via
a single POST /mcp endpoint with SSE streaming responses.

Usage:
    python adapter.py --port 12020 --host 127.0.0.1 -- npx -y wikipedia-mcp
"""

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from logging_config import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await _kill_subprocess()


app = FastAPI(title="MCProxy Adapter", lifespan=_lifespan)

_process: Optional[asyncio.subprocess.Process] = None
_stderr_task: Optional[asyncio.Task] = None
_stdio_lock = asyncio.Lock()
_session_id: Optional[str] = None
_die_with_parent: bool = False


async def _drain_stderr() -> None:
    if _process is None or _process.stderr is None:
        return
    try:
        while True:
            line = await _process.stderr.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if line_str:
                logger.debug(f"[adapter stderr] {line_str[:200]}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"[adapter stderr drain ended: {e}]")


async def _send_message(message: dict) -> None:
    if _process is None or _process.stdin is None:
        raise RuntimeError("Subprocess is not running")
    data = json.dumps(message) + "\n"
    _process.stdin.write(data.encode())
    await _process.stdin.drain()


async def _read_message() -> Optional[dict]:
    if _process is None or _process.stdout is None:
        raise RuntimeError("Subprocess is not running")

    buffer = ""
    max_lines = 100
    line_count = 0

    try:
        while line_count < max_lines:
            try:
                line = await asyncio.wait_for(_process.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                if buffer:
                    break
                continue

            line_count += 1

            if not line:
                if buffer:
                    break
                return None

            line_str = line.decode("utf-8", errors="replace").strip()

            if not line_str:
                continue

            if not line_str.startswith(("{", "[")):
                logger.debug(f"Skipping non-JSON line: {line_str[:100]}...")
                continue

            if buffer:
                buffer += "\n"
            buffer += line_str

            try:
                return json.loads(buffer)
            except json.JSONDecodeError:
                continue

        if buffer:
            logger.error(
                f"Failed to parse JSON after {line_count} lines: {buffer[:500]}"
            )
        return None

    except Exception as e:
        logger.error(f"Error reading message: {e}")
        return None


async def _ensure_subprocess(command: list[str], env: dict) -> None:
    global _process, _stderr_task

    if _process is not None and _process.returncode is None:
        return

    logger.info(f"Starting subprocess: {' '.join(command)}")

    _process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=1024 * 1024,
    )

    _stderr_task = asyncio.create_task(_drain_stderr())


async def _kill_subprocess() -> None:
    global _process, _stderr_task

    if _stderr_task is not None:
        _stderr_task.cancel()
        try:
            await _stderr_task
        except asyncio.CancelledError:
            pass
        _stderr_task = None

    if _process is None:
        return

    try:
        if _process.returncode is None:
            _process.terminate()
            try:
                await asyncio.wait_for(_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Subprocess did not terminate, killing")
                _process.kill()
                await _process.wait()
    except Exception as e:
        logger.error(f"Error stopping subprocess: {e}")
    finally:
        _process = None


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    global _session_id

    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": "Parse error"},
                    "id": None,
                }
            ),
            media_type="application/json",
            status_code=400,
        )

    incoming_session = request.headers.get("mcp-session-id")
    if incoming_session and _session_id and incoming_session != _session_id:
        return Response(
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "Invalid session"},
                    "id": body.get("id"),
                }
            ),
            media_type="application/json",
            status_code=400,
        )

    command: list[str] = app.state.command
    env: dict = app.state.env

    async def _stream_response():
        global _session_id

        async with _stdio_lock:
            if _process is None or _process.returncode is not None:
                try:
                    await _ensure_subprocess(command, env)
                except Exception as e:
                    yield f"data: {json.dumps({'jsonrpc': '2.0', 'error': {'code': -32000, 'message': str(e)}, 'id': body.get('id')})}\n\n"
                    return

            try:
                await _send_message(body)
            except Exception as e:
                yield f"data: {json.dumps({'jsonrpc': '2.0', 'error': {'code': -32000, 'message': f'Failed to send to subprocess: {e}'}, 'id': body.get('id')})}\n\n"
                return

            is_notification = "id" not in body
            if is_notification:
                return

            response = await asyncio.wait_for(_read_message(), timeout=350)

            if response is None:
                if _process and _process.returncode is not None:
                    yield f"data: {json.dumps({'jsonrpc': '2.0', 'error': {'code': -32000, 'message': 'Subprocess terminated unexpectedly'}, 'id': body.get('id')})}\n\n"
                else:
                    yield f"data: {json.dumps({'jsonrpc': '2.0', 'error': {'code': -32000, 'message': 'No response from subprocess'}, 'id': body.get('id')})}\n\n"
                return

            # ponytail: guard against broken-flush stdio servers that return a
            # previous request's response one call late (e.g. community
            # perplexity-mcp). Without this, mcproxy silently surfaces wrong
            # data — an initialize response for a tools/list call. Skew on the
            # JSON-RPC id is the tell; error loudly instead.
            expected_id = body.get("id")
            actual_id = response.get("id")
            if actual_id is not None and actual_id != expected_id:
                logger.error(
                    f"[adapter] id mismatch: expected {expected_id!r}, got "
                    f"{actual_id!r} (subprocess likely buffers stdout; "
                    f"responses arriving one request late)"
                )
                err = {
                    "jsonrpc": "2.0",
                    "id": expected_id,
                    "error": {
                        "code": -32000,
                        "message": (
                            f"Adapter id mismatch: expected {expected_id!r}, "
                            f"received {actual_id!r}. The wrapped subprocess "
                            f"returned a response for a different request "
                            f"(likely a stdout buffering bug; responses "
                            f"arriving one request late)."
                        ),
                    },
                }
                yield f"data: {json.dumps(err)}\n\n"
                return

            yield f"data: {json.dumps(response)}\n\n"

    headers = {}
    if _session_id:
        headers["mcp-session-id"] = _session_id

    return StreamingResponse(
        _stream_response(),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/health")
async def health() -> dict:
    alive = _process is not None and _process.returncode is None
    return {
        "status": "ok" if alive else "degraded",
        "subprocess": "running" if alive else "stopped",
    }


def _watch_parent():
    if not _die_with_parent:
        return

    ppid = os.getppid()

    async def _poll():
        while True:
            await asyncio.sleep(1)
            try:
                os.kill(ppid, 0)
            except ProcessLookupError:
                logger.info("Parent process died, shutting down")
                await _kill_subprocess()
                os._exit(1)

    asyncio.create_task(_poll())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCProxy stdio-to-HTTP adapter",
        usage="%(prog)s [options] -- <command> [args...]",
    )
    parser.add_argument(
        "--port", type=int, default=12020, help="HTTP port (default: 12020)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--die-with-parent", action="store_true", help="Exit when parent process dies"
    )

    args, remaining = parser.parse_known_args()

    if not remaining or remaining[0] != "--":
        parser.error("Must specify command after -- (e.g., -- npx -y wikipedia-mcp)")

    command = remaining[1:]
    if not command:
        parser.error("Must specify a command after --")

    setup_logging(use_stderr=True)

    logger.info(
        f"Adapter starting on {args.host}:{args.port} for command: {' '.join(command)}"
    )

    global _die_with_parent
    _die_with_parent = args.die_with_parent

    app.state.command = command
    app.state.env = dict(os.environ)

    if _die_with_parent:
        _watch_parent()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.run(_kill_subprocess())


if __name__ == "__main__":
    main()
