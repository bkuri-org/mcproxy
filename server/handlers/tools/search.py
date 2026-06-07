"""Meta-tool search handler."""

import json
from typing import Any, Dict, Optional

from manifest import CapabilityRegistry, ManifestQuery
from manifest.example_gen import generate_tool_example
from logging_config import get_logger

logger = get_logger(__name__)


def _enrich_single_match(
    results: Dict[str, Any],
    capability_registry: Optional[CapabilityRegistry] = None,
    namespace: Optional[str] = None,
) -> None:
    """When exactly 1 tool matches, attach full schema as best_match.

    Mutates results in-place. Does nothing for 0 or 2+ tool matches.

    Args:
        results: Search results dict from ManifestQuery.search()
        capability_registry: Capability registry for full tool lookup
        namespace: Namespace filter (passed to get_tools)
    """
    if capability_registry is None or not capability_registry._manifest:
        return

    tool_matches = results.get("matches", {}).get("tools", [])
    if len(tool_matches) != 1:
        return

    # Parse "server:tool_name" from the single match
    match_key = tool_matches[0]
    parts = match_key.split(":", 1)
    if len(parts) != 2:
        return
    server_name, tool_name = parts

    # Look up full tool data from manifest
    tools = capability_registry.get_tools(server_name, namespace)
    full_tool = None
    for t in tools:
        if t.get("name") == tool_name:
            full_tool = t
            break

    if full_tool is None:
        return

    input_schema = full_tool.get("inputSchema")
    description = full_tool.get("description", "")
    usage_example = generate_tool_example(server_name, tool_name, input_schema)

    results["best_match"] = {
        "server": server_name,
        "name": tool_name,
        "description": description,
        "inputSchema": input_schema,
        "usage_example": usage_example,
    }


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

    When exactly 1 tool matches the query, the response includes a
    ``best_match`` key with the tool's full ``inputSchema``, description,
    and a ``usage_example`` -- saving the agent a separate inspect call.

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

        # Auto-return full details for single tool match
        _enrich_single_match(
            results, capability_registry, effective_namespace
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
