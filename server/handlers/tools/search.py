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
    min_words: int = 2,
    max_tools: int = 5,
) -> Dict[str, Any]:
    """Handle search meta-tool.

    Supports hierarchical depth levels for progressive detail:
        0: Server names only
        1: Server names + categories + tool counts
        2: Tool names + descriptions (default for long queries)
        3: Tool names + descriptions + inputSchema + usage examples

    At depth=3 (set via max_depth=3 param), each matched tool includes
    its full inputSchema and a generated usage example like:
        api.server('wikipedia').search(query='<query>', limit=5)

    Args:
        msg_id: JSON-RPC message ID
        params: Search parameters (query, namespace, max_depth, max_results)
        connection_namespace: Namespace from connection context (X-Namespace header)
        capability_registry: Capability registry instance
        min_words: Minimum words to trigger depth=2 (default: 2)
        max_tools: Maximum tools to return per server (default: 5)

    Returns:
        MCP response with search results
    """
    query = params.get("query", "")

    param_namespace = params.get("namespace")
    effective_namespace = param_namespace or connection_namespace
    # Get depth override from params (support both 'depth' and 'max_depth')
    effective_depth = params.get("max_depth") or params.get("depth")
    # Get max_results override (overrides config default)
    effective_max_tools = params.get("max_results", max_tools)
    # Get brief mode - handle string representations properly
    brief_param = params.get("brief", False)
    if isinstance(brief_param, str):
        brief = brief_param.lower() in ("true", "1")
    else:
        brief = bool(brief_param)

    # Count words in query
    query_words = query.strip().split() if query else []

    # Default to depth=1 for empty/short queries (concise), depth=2 for specific queries
    # min_words <= 0 means always use depth=2
    if min_words <= 0:
        default_depth = 2  # Always show schemas
    else:
        default_depth = 1 if not query or len(query_words) < min_words else 2
    max_depth = effective_depth if effective_depth is not None else default_depth

    # If brief mode, force depth=1
    if brief:
        max_depth = 1

    log_ns = f" namespace={effective_namespace}" if effective_namespace else ""
    logger.debug(
        f"[SEARCH] query={query}{log_ns} max_depth={max_depth} max_results={effective_max_tools} brief={brief}"
    )

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
            max_depth=max_depth,
            max_tools=effective_max_tools,
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
