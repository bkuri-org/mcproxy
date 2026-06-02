"""Query interface for manifest data."""

from typing import Any, Dict, Optional

from utils.fuzzy_match import fuzzy_score

from .registry import CapabilityRegistry
from .example_gen import generate_tool_example


class ManifestQuery:
    """Query interface for manifest data.

    Supports hierarchical search with depth levels and fuzzy matching.
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        """Initialize query interface.

        Args:
            registry: CapabilityRegistry instance to query
        """
        self._registry = registry

    def search(
        self,
        query: str,
        namespace: Optional[str] = None,
        max_depth: int = 2,
        max_tools: int = 5,
    ) -> Dict:
        """Search manifest with fuzzy matching.

        Depth levels:
            0: Server names only
            1: Server names + categories + tool counts
            2: Server names + categories + tool names + descriptions
            3: Tool names + descriptions + inputSchema + usage examples

        At depth=3, each matched tool includes its full inputSchema and a
        generated usage example like:
            api.server('wikipedia').search(query='<query>', limit=5)

        Args:
            query: Search query string
            namespace: Optional namespace filter
            max_depth: Maximum depth level (0-3)
            max_tools: Maximum tools to return at depth=2 (default: 5)

        Returns:
            Search results dictionary
        """
        manifest = self._registry._manifest
        if not manifest:
            return {"error": "Manifest not built", "results": []}

        # Check query cache
        cached = self._registry.query_cache.get(query, namespace, max_depth, max_tools)
        if cached is not None:
            return cached

        results: Dict[str, Any] = {
            "query": query,
            "namespace": namespace,
            "max_depth": max_depth,
            "results": [],
            "matches": {
                "servers": [],
                "categories": [],
                "tools": [],
            },
        }

        servers = self._registry.get_servers(namespace)
        query_lower = query.lower() if query else ""
        min_similarity = 0.4

        # At depth=2, limit results to prevent token explosion
        max_tools_at_depth_2 = max_tools

        show_all = max_depth >= 1 and (not query_lower or len(query_lower) <= 1)

        for server_name in servers:
            if show_all:
                server_match_score = 1.0
            else:
                server_match_score = fuzzy_score(
                    query_lower, server_name.lower(), min_similarity
                )

            # Always check if we should include this server (not just in else block)
            if server_match_score >= min_similarity or max_depth >= 1 or show_all:
                server_entry: Dict[str, Any] = {
                    "server": server_name,
                    "match_score": server_match_score,
                }

                if server_match_score >= min_similarity:
                    results["matches"]["servers"].append(server_name)

                    if max_depth >= 1:
                        server_info = manifest.get("servers", {}).get(server_name, {})
                        categories = server_info.get("categories", [])
                        matched_categories = []

                        for cat in categories:
                            cat_score = fuzzy_score(
                                query_lower, cat.lower(), min_similarity
                            )
                            if cat_score >= min_similarity:
                                matched_categories.append(cat)
                                results["matches"]["categories"].append(
                                    f"{server_name}:{cat}"
                                )

                        server_entry["categories"] = categories
                        server_entry["matched_categories"] = matched_categories

                        # Always include tool count at depth >= 1
                        if namespace:
                            tools = self._registry.get_tools(server_name, namespace)
                        else:
                            tools = self._registry.get_tools(server_name)
                        server_entry["tools"] = len(tools)

                        # Search tool names even at depth=1 (for discoverability)
                        if not show_all and query_lower:
                            for tool in tools:
                                tool_name = tool.get("name", "")
                                name_score = fuzzy_score(
                                    query_lower, tool_name.lower(), min_similarity
                                )
                                if name_score >= min_similarity:
                                    results["matches"]["tools"].append(
                                        f"{server_name}:{tool_name}"
                                    )

                if max_depth >= 2:
                    tools = self._registry.get_tools(server_name, namespace)
                    matched_tools = []

                    for tool in tools:
                        tool_name = tool.get("name", "")
                        tool_desc = tool.get("description", "")

                        name_score = fuzzy_score(
                            query_lower, tool_name.lower(), min_similarity
                        )
                        desc_score = fuzzy_score(
                            query_lower, tool_desc.lower(), min_similarity * 0.7
                        )

                        best_score = max(name_score, desc_score)
                        if best_score >= min_similarity:
                            tool_match = {
                                "name": tool_name,
                                "match_score": best_score,
                            }

                            # At depth >= 2, include truncated description
                            if max_depth >= 2:
                                tool_match["description"] = (
                                    tool_desc[:200] if tool_desc else ""
                                )  # Truncate long descriptions

                            if max_depth >= 3:
                                # Full description + inputSchema + usage example
                                tool_match["description"] = tool_desc
                                tool_match["inputSchema"] = tool.get("inputSchema", {})
                                tool_match["example"] = generate_tool_example(
                                    server_name,
                                    tool_name,
                                    tool.get("inputSchema", {}),
                                )

                            matched_tools.append(tool_match)
                            results["matches"]["tools"].append(
                                f"{server_name}:{tool_name}"
                            )

                    server_entry["tools"] = len(tools)
                    server_entry["matched_tools"] = matched_tools

                # Limit results at depth >= 2 to prevent token explosion
                # max_tools <= 0 means unlimited (show all)
                if (
                    max_depth >= 2
                    and max_tools_at_depth_2 > 0
                    and len(server_entry.get("matched_tools", []))
                    > max_tools_at_depth_2
                ):
                    server_entry["matched_tools"] = server_entry["matched_tools"][
                        :max_tools_at_depth_2
                    ]
                    server_entry["_truncated"] = True
                    server_entry["_total_matched"] = len(matched_tools)

                should_include = (
                    server_match_score >= min_similarity
                    or server_entry.get("matched_categories")
                    or server_entry.get("matched_tools")
                    or show_all
                )

                if should_include:
                    results["results"].append(server_entry)

        results["total_matches"] = sum(
            len(results["matches"][k]) for k in results["matches"]
        )

        # Add warning if results were truncated at depth >= 2
        if max_depth >= 2:
            truncated_servers = [r for r in results["results"] if r.get("_truncated")]
            if truncated_servers:
                results["warning"] = (
                    f"Results limited to {max_tools_at_depth_2} tools per server. "
                    f"Use a more specific query to narrow results, or use max_depth=1 for overview. "
                    f"Truncated: {', '.join(f'{r["server"]} ({r["_total_matched"]} tools)' for r in truncated_servers)}"
                )

        # Cache the result
        self._registry.query_cache.set(query, namespace, max_depth, max_tools, results)

        return results
