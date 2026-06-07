"""Meta-tool search handler."""

import json
from typing import Any, Dict, Optional

from manifest import CapabilityRegistry, ManifestQuery
from logging_config import get_logger

logger = get_logger(__name__)


async def handle_search(
    msg_id: Any,
    params: Dict,
    connection_namespace: Optional[str] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
) -> Dict[str, Any]:
    """Handle search meta-tool.

    Behavior is automatic based on query:
        - Empty/whitespace query: server list with tool counts (overview)
        - Query provided: matching tools with truncated descriptions

    Args:
        msg_id: JSON-RPC message ID
        params: Search parameters (query, namespace)
        connection_namespace: Namespace from connection context (X-Namespace header)
        capability_registry: Capability registry instance

    Returns:
        MCP response with search results
    """
    query = params.get("query", "")

    param_namespace = params.get("namespace")
    effective_namespace = param_namespace or connection_namespace

    log_ns = f" namespace={effective_namespace}" if effective_namespace else ""
    logger.debug(f"[SEARCH] query={query}{log_ns}")

    try:
        if capability_registry is None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32000,
                    "message": "Capability registry not initialized",
                },
            }

        mq = ManifestQuery(capability_registry)
        results = mq.search(
            query,
            namespace=effective_namespace,
        )

        if not effective_namespace:
            results["warning"] = (
                "No namespace specified. Results include default servers only. "
                "Isolated namespaces (e.g., 'system', 'home') require explicit namespace parameter."
            )

        content = [{"type": "text", "text": json.dumps(results)}]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content}}

    except Exception as e:
        logger.error(f"[SEARCH_ERROR] {e}")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32000, "message": f"Search failed: {e}"},
        }
