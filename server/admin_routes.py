"""Admin API endpoints for MCProxy."""

import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import AgentRegistry
from auth.audit_logger import AuditLogger
from logging_config import get_logger

logger = get_logger(__name__)

_audit_logger = AuditLogger()

router = APIRouter(prefix="/admin", tags=["admin"])

_blocklist_refresh_last: dict[str, float] = {}
_BLOCKLIST_REFRESH_WINDOW = 60.0


class AgentResponse(BaseModel):
    agent_id: str
    client_id: str
    allowed_scopes: list[str]
    namespace: str
    enabled: bool
    created_at: str
    updated_at: str


class RotateResponse(BaseModel):
    client_id: str
    client_secret: Optional[str] = None
    reauth_required: bool


def get_agent_registry(request: Request) -> AgentRegistry:
    """Get agent registry from app state."""
    registry = getattr(request.app.state, "agent_registry", None)
    if not registry:
        raise HTTPException(status_code=500, detail="Agent registry not configured")
    return registry


def get_auth_config(request: Request) -> dict:
    """Get auth config from app state."""
    return getattr(request.app.state, "auth_config", {})


def get_blocklist_sync(request: Request):
    """Get BlocklistSync manager from app state."""
    sync = getattr(request.app.state, "blocklist_sync", None)
    if not sync:
        raise HTTPException(status_code=503, detail="Blocklist sync not configured")
    return sync


def admin_auth(request: Request) -> bool:
    """Validate admin access.

    Logic:
    - If MCPROXY_ADMIN_KEY env var not set: localhost only (127.0.0.1)
    - If set: require X-Admin-Key header matching the env var
      (but still allow localhost as a fallback if no key provided)
    """
    auth_config = get_auth_config(request)
    admin_key_env = auth_config.get("admin_key_env", "MCPROXY_ADMIN_KEY")
    admin_key = os.environ.get(admin_key_env)
    client_host = request.client.host if request.client else None

    if not admin_key:
        # No key configured - allow localhost only
        if client_host == "127.0.0.1":
            return True
        raise HTTPException(
            status_code=403,
            detail=(
                "Admin key not configured. Set MCPROXY_ADMIN_KEY environment variable "
                "for production deployments, or access from localhost."
            ),
        )

    # Key IS configured - require it for non-localhost requests
    # For localhost, allow without key (backward compatibility)
    if client_host == "127.0.0.1":
        provided = request.headers.get("X-Admin-Key")
        if provided != admin_key and provided is not None:
            raise HTTPException(status_code=401, detail="Invalid admin key")
        # Allow localhost with or without key
        return True

    # Non-localhost: require key
    provided = request.headers.get("X-Admin-Key")
    if provided != admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    return True


@router.get("/agents")
async def list_agents(
    request: Request,
    namespace: Optional[str] = None,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """List all agents.

    Args:
        namespace: Optional namespace filter
    """
    try:
        agents = registry.list_agents(namespace=namespace, enabled_only=False)
        return JSONResponse(content={"agents": agents})
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{agent_id}")
async def get_agent(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Get agent details by ID."""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return JSONResponse(
        content={
            "agent_id": agent.agent_id,
            "client_id": agent.client_id,
            "allowed_scopes": list(agent.allowed_scopes),
            "namespace": agent.namespace,
            "enabled": agent.enabled,
            "created_at": agent.created_at.isoformat(),
            "updated_at": agent.updated_at.isoformat(),
        }
    )


@router.post("/agents/{agent_id}/rotate")
async def rotate_agent_secret(
    request: Request,
    agent_id: str,
    reauth: bool = False,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Rotate an agent's client secret.

    Args:
        agent_id: Agent ID to rotate
        reauth: If true, don't return new secret - require re-registration
    """
    auth_config = get_auth_config(request)
    rotate_reauth = auth_config.get("rotate_reauth", False)

    use_reauth = reauth if reauth else rotate_reauth

    result = registry.rotate_secret(agent_id)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found")

    if use_reauth:
        logger.info(f"Rotated secret for agent {agent_id} (reauth required)")
        _audit_logger.log_agent_rotated(agent_id, reauth_mode=True)
        return JSONResponse(
            content={
                "client_id": result["client_id"],
                "reauth_required": True,
                "message": "Secret rotated. Agent must re-register to obtain new credentials.",
            }
        )

    logger.info(f"Rotated secret for agent {agent_id}")
    _audit_logger.log_agent_rotated(agent_id, reauth_mode=False)
    return JSONResponse(
        content={
            "client_id": result["client_id"],
            "client_secret": result["client_secret"],
            "reauth_required": False,
        }
    )


@router.post("/agents/{agent_id}/enable")
async def enable_agent(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Enable a disabled agent."""
    success = registry.enable(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")

    logger.info(f"Enabled agent {agent_id}")
    _audit_logger.log_agent_enabled(agent_id)
    return JSONResponse(content={"agent_id": agent_id, "enabled": True})


@router.post("/agents/{agent_id}/disable")
async def disable_agent(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Disable an agent."""
    success = registry.disable(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")

    logger.info(f"Disabled agent {agent_id}")
    _audit_logger.log_agent_disabled(agent_id)
    return JSONResponse(content={"agent_id": agent_id, "enabled": False})


@router.delete("/agents/{agent_id}")
async def delete_agent(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Delete an agent."""
    success = registry.delete(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")

    logger.info(f"Deleted agent {agent_id}")
    _audit_logger.log_agent_deleted(agent_id)
    return JSONResponse(content={"agent_id": agent_id, "deleted": True})


@router.get("/agents/{agent_id}/api-key")
async def get_agent_api_key(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Get current API key for an agent."""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    has_api_key = bool(agent.api_key)
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "has_api_key": has_api_key,
        }
    )


@router.post("/agents/{agent_id}/api-key")
async def generate_api_key(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Generate a new API key for an agent."""
    result = registry.rotate_api_key(agent_id)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found")

    logger.info(f"Generated new API key for agent {agent_id}")
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "api_key": result,
            "message": "API key generated. Store securely - it cannot be retrieved again once you leave this page.",
        }
    )


@router.delete("/agents/{agent_id}/api-key")
async def revoke_api_key(
    request: Request,
    agent_id: str,
    registry: AgentRegistry = Depends(get_agent_registry),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Revoke API key for an agent."""
    result = registry.rotate_api_key(agent_id)
    if not result:
        raise HTTPException(status_code=404, detail="Agent not found")

    logger.info(f"Revoked API key for agent {agent_id}")
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "api_key": None,
            "message": "API key revoked. Generate a new key to restore access.",
        }
    )


@router.post("/blocklist/refresh")
async def refresh_blocklist(
    request: Request,
    sync=Depends(get_blocklist_sync),
    _: bool = Depends(admin_auth),
) -> JSONResponse:
    """Force a blocklist sync refresh. Rate-limited to 1 request per 60s."""
    client_host = request.client.host if request.client else "unknown"
    now = time.monotonic()
    last = _blocklist_refresh_last.get(client_host, 0.0)
    if now - last < _BLOCKLIST_REFRESH_WINDOW:
        remaining = _BLOCKLIST_REFRESH_WINDOW - (now - last)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Retry after {remaining:.1f}s",
        )
    _blocklist_refresh_last[client_host] = now
    try:
        await sync.refresh()
    except Exception as e:
        logger.error(f"Blocklist refresh failed: {e}")
        raise HTTPException(status_code=502, detail=f"Blocklist refresh failed: {e}")
    return JSONResponse(
        content={"status": "ok", "message": "Blocklist refresh triggered"}
    )


def register_admin_routes(
    app, agent_registry: AgentRegistry, auth_config: dict
) -> None:
    """Register admin routes with the FastAPI app."""
    app.state.agent_registry = agent_registry
    app.state.auth_config = auth_config
    app.include_router(router)
