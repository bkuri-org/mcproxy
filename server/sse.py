"""SSE endpoints and event streaming for MCProxy."""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from logging_config import get_logger

logger = get_logger(__name__)
security = HTTPBearer(auto_error=False)


def validate_namespace(namespace: str, capability_registry: Optional[Any]) -> bool:
    """Validate that a namespace or group exists in the registry.

    Args:
        namespace: Namespace or group name to validate
        capability_registry: Capability registry instance

    Returns:
        True if namespace/group exists, False otherwise
    """
    if capability_registry is None:
        return False
    servers, error = capability_registry.resolve_namespace_to_servers(namespace)
    return error is None


def get_namespace_from_request(request: Request) -> Optional[str]:
    """Extract namespace from request headers.

    Args:
        request: FastAPI request object

    Returns:
        Namespace name from X-Namespace header, or None
    """
    return request.headers.get("X-Namespace")


def get_session_id_from_request(request: Request) -> Optional[str]:
    """Extract session ID from request headers.

    Args:
        request: FastAPI request object

    Returns:
        Session ID from X-Session-ID header, or None
    """
    return request.headers.get("X-Session-ID")


def resolve_default_namespace(capability_registry: Optional[Any]) -> str:
    """Get the default namespace/group name.

    Checks groups first (preferred), then namespaces. This allows /sse
    (bare endpoint) to resolve to the "default" group.

    Args:
        capability_registry: Capability registry instance

    Returns:
        Default namespace/group name (empty string if no default set)
    """
    if capability_registry is None:
        return ""
    groups = capability_registry._groups
    if "default" in groups:
        return "default"
    namespaces = capability_registry._namespaces
    if "default" in namespaces:
        return "default"
    if "public" in namespaces:
        return "public"
    return ""


def check_auth(request: Request) -> Optional[Dict[str, Any]]:
    """Check authentication if enabled.

    Args:
        request: FastAPI request object

    Returns:
        Auth context dict if authenticated, None if auth disabled

    Raises:
        HTTPException: If auth is required but missing/invalid
    """
    auth_config = getattr(request.app.state, "auth_config", None)

    if not auth_config or not auth_config.get("enabled", False):
        return None

    from server.auth_middleware import static_key_auth

    try:
        auth_info = static_key_auth(request)
        return {
            "agent_id": auth_info.get("agent_id"),
            "scopes": auth_info.get("scopes", []),
            "namespace": auth_info.get("namespace"),
            "tenant_id": None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Auth validation failed: {e}")
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )


async def sse_event_stream(
    request: Request, namespace: Optional[str], log_prefix: str
) -> AsyncGenerator[str, None]:
    """Generate SSE events for MCP connections.

    Args:
        request: FastAPI request object for disconnect detection
        namespace: Optional namespace context
        log_prefix: Log prefix string (e.g., "[SSE]" or "[SSE_NAMESPACE]")

    Yields:
        SSE formatted event strings
    """
    ns_info = f" namespace={namespace}" if namespace else ""
    try:
        # MCP SSE transport spec: endpoint event data MUST be the URI path,
        # not a JSON object. The SDK does urljoin(base_url, sse.data).
        # Advertise the namespaced POST path so spec-compliant clients (which
        # POST wherever `endpoint` says) keep the namespace binding without
        # having to echo X-Namespace on every request. The /sse/{ns} POST
        # handler exists and threads path_namespace into handle_message.
        endpoint_path = f"/sse/{namespace}" if namespace else "/message"
        yield f"event: endpoint\ndata: {endpoint_path}\n\n"
        if namespace:
            yield f"event: namespace\ndata: {namespace}\n\n"

        while True:
            if await request.is_disconnected():
                logger.info(f"{log_prefix} Client disconnected{ns_info}")
                break

            await asyncio.sleep(30)
            heartbeat_data: Dict[str, Any] = {
                "timestamp": asyncio.get_event_loop().time()
            }
            if namespace:
                heartbeat_data["namespace"] = namespace
            yield f"event: heartbeat\ndata: {json.dumps(heartbeat_data)}\n\n"

    except asyncio.CancelledError:
        logger.info(f"{log_prefix} Connection cancelled{ns_info}")
    except Exception as e:
        logger.error(f"{log_prefix} Error{ns_info}: {e}")


def register_sse_endpoints(
    app,
    capability_registry_getter,
    handle_message,
) -> None:
    """Register SSE endpoints on the FastAPI app.

    Args:
        app: FastAPI application instance
        capability_registry_getter: Callable that returns the capability registry
        handle_message: Async function to handle MCP messages
    """

    @app.get("/sse/{namespace}")
    async def sse_endpoint_namespaced(
        namespace: str, request: Request
    ) -> StreamingResponse:
        """SSE endpoint with namespace isolation."""
        capability_registry = capability_registry_getter()
        if not validate_namespace(namespace, capability_registry):
            logger.warning(f"[SSE_NAMESPACE] Invalid namespace: {namespace}")
            raise HTTPException(
                status_code=404, detail=f"Namespace not found: {namespace}"
            )

        header_ns = get_namespace_from_request(request)
        effective_ns = header_ns if header_ns else namespace

        if header_ns and header_ns != namespace:
            logger.warning(
                f"[SSE_NAMESPACE] URL namespace '{namespace}' overridden by header '{header_ns}'"
            )
            if not validate_namespace(header_ns, capability_registry):
                raise HTTPException(
                    status_code=404, detail=f"Namespace not found: {header_ns}"
                )
            effective_ns = header_ns

        logger.info(
            f"[SSE_NAMESPACE] New connection from {request.client} namespace={effective_ns}"
        )

        return StreamingResponse(
            sse_event_stream(request, effective_ns, "[SSE_NAMESPACE]"),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Namespace": effective_ns,
            },
        )

    @app.get("/sse")
    async def sse_endpoint(request: Request) -> StreamingResponse:
        """SSE endpoint for MCP protocol."""
        capability_registry = capability_registry_getter()
        header_ns = get_namespace_from_request(request)
        default_ns = resolve_default_namespace(capability_registry)
        effective_ns = header_ns if header_ns else default_ns

        if header_ns and not validate_namespace(header_ns, capability_registry):
            logger.warning(f"[SSE] Invalid X-Namespace header: {header_ns}")
            raise HTTPException(
                status_code=404, detail=f"Namespace not found: {header_ns}"
            )

        ns_info = f" namespace={effective_ns}" if effective_ns else ""
        logger.info(f"[SSE] New connection from {request.client}{ns_info}")

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
        if effective_ns:
            headers["X-Namespace"] = effective_ns

        return StreamingResponse(
            sse_event_stream(request, effective_ns, "[SSE]"),
            media_type="text/event-stream",
            headers=headers,
        )

    @app.post("/sse")
    async def handle_sse_message(request: Request) -> Dict[str, Any]:
        """Handle MCP POST messages at /sse (for OpenCode compatibility).

        Resolves the default group/namespace if no X-Namespace header is provided.
        """
        check_auth(request)
        header_ns = get_namespace_from_request(request)
        if not header_ns:
            capability_registry = capability_registry_getter()
            default_ns = resolve_default_namespace(capability_registry)
            if default_ns:
                return await handle_message(request, path_namespace=default_ns)
        return await handle_message(request)

    @app.post("/sse/{namespace}")
    async def handle_sse_message_namespaced(
        namespace: str, request: Request
    ) -> Dict[str, Any]:
        """Handle MCP POST messages at /sse/{namespace} for namespaced access."""
        capability_registry = capability_registry_getter()
        if not validate_namespace(namespace, capability_registry):
            raise HTTPException(
                status_code=404, detail=f"Namespace not found: {namespace}"
            )
        check_auth(request)
        return await handle_message(request, path_namespace=namespace)
