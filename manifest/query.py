"""Query interface for manifest data."""

from typing import Any, Dict, Optional

from utils.fuzzy_match import fuzzy_score

from .registry import CapabilityRegistry


class ManifestQuery:
    """Query interface for manifest data.

    Supports automatic search depth: empty query returns server overview,
    provided query returns matching tools with descriptions.
    Matches against both bare tool names and server-prefixed names
    (e.g., both 'toggle' and 'home_assistant__toggle').
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
    ) -> Dict:
        """Search manifest with fuzzy matching.

        Behavior is automatic based on query:
            - Empty/whitespace query: server list with tool counts (overview)
            - Query provided: matching tools with truncated descriptions

        Matches against both bare tool names (e.g., 'toggle') and
        prefixed names (e.g., 'home_assistant__toggle') so clients
        that see prefixed names in tools/list can search by those names.

        When a query matches a server name, all tools for that server
        are returned (expanding collapsed servers that would show as
        'server__*' wildcards in tools/list).

        Args:
            query: Search query string
            namespace: Optional namespace filter

        Returns:
            Search results dictionary
        """
        manifest = self._registry._manifest
        if not manifest:
            return {"error": "Manifest not built", "results": []}

        # Check query cache
        cached = self._registry.query_cache.get(query, namespace)
        if cached is not None:
            return cached

        query_stripped = query.strip()
        show_all = not query_stripped
        query_lower = query_stripped.lower()
        min_similarity = 0.4

        results: Dict[str, Any] = {
            "query": query,
            "namespace": namespace,
            "results": [],
            "matches": {
                "servers": [],
                "categories": [],
                "tools": [],
            },
        }

        servers = self._registry.get_servers(namespace)

        for server_name in servers:
            if show_all:
                server_match_score = 1.0
            else:
                server_match_score = fuzzy_score(
                    query_lower, server_name.lower(), min_similarity
                )

            if server_match_score >= min_similarity or show_all:
                # Server matched by name or overview mode
                server_entry: Dict[str, Any] = {
                    "server": server_name,
                    "match_score": server_match_score,
                }

                if server_match_score >= min_similarity:
                    results["matches"]["servers"].append(server_name)

                # Get categories
                server_info = manifest.get("servers", {}).get(server_name, {})
                categories = server_info.get("categories", [])
                matched_categories = []

                if not show_all:
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
                if not show_all:
                    server_entry["matched_categories"] = matched_categories

                # Get tools
                tools = self._registry.get_tools(
                    server_name, namespace
                )
                server_entry["tools"] = len(tools)

                if show_all:
                    # Overview mode: just server names with counts
                    results["results"].append(server_entry)
                    continue

                # Server matched - expand all tools (collapsed server expansion)
                matched_tools = []
                for tool in tools:
                    tool_name = tool.get("name", "")
                    tool_desc = tool.get("description", "")
                    tool_match = {"name": tool_name, "match_score": 1.0}
                    if tool_desc:
                        tool_match["description"] = tool_desc[:200]
                    matched_tools.append(tool_match)
                    results["matches"]["tools"].append(
                        f"{server_name}:{tool_name}"
                    )

                server_entry["matched_tools"] = matched_tools
                results["results"].append(server_entry)
                continue

            # Server name didn't match - check tools and categories
            tools = self._registry.get_tools(server_name, namespace)
            if not tools:
                continue

            server_entry: Dict[str, Any] = {
                "server": server_name,
                "match_score": server_match_score,
                "tools": len(tools),
            }

            # Get categories
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

            # Match tools against bare name, prefixed name, and description
            matched_tools = []
            for tool in tools:
                tool_name = tool.get("name", "")
                tool_desc = tool.get("description", "")

                prefixed_name = f"{server_name}__{tool_name}"
                name_score = max(
                    fuzzy_score(query_lower, tool_name.lower(), min_similarity),
                    fuzzy_score(query_lower, prefixed_name.lower(), min_similarity),
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
                    if tool_desc:
                        tool_match["description"] = tool_desc[:200]

                    matched_tools.append(tool_match)
                    results["matches"]["tools"].append(
                        f"{server_name}:{tool_name}"
                    )

            server_entry["matched_tools"] = matched_tools

            should_include = (
                server_entry.get("matched_categories")
                or server_entry.get("matched_tools")
            )

            if should_include:
                results["results"].append(server_entry)

        results["total_matches"] = sum(
            len(results["matches"][k]) for k in results["matches"]
        )

        # Cache the result
        self._registry.query_cache.set(query, namespace, results)

        return results
