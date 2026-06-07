"""Capability registry for building and managing manifests."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from logging_config import get_logger
from utils.namespace import normalize_namespace_config
from .errors import NamespaceInheritanceError

logger = get_logger(__name__)

CACHE_DIR = Path("./cache")
CACHE_FILE = CACHE_DIR / "manifest.json"
CACHE_TTL_SECONDS = 3600


class QueryResultCache:
    """Simple in-memory cache for search query results with TTL.

    Cache entries are keyed by a string combining (query, namespace).
    Expired entries are lazily evicted on read.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        """Initialize the query result cache.

        Args:
            ttl_seconds: Time-to-live in seconds for cache entries (default: 300)
        """
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._ttl = ttl_seconds

    @property
    def ttl(self) -> int:
        """Get current TTL in seconds."""
        return self._ttl

    @ttl.setter
    def ttl(self, value: int) -> None:
        """Set TTL. Setting to 0 effectively disables caching."""
        self._ttl = value
        if value <= 0:
            self.clear()

    def _make_key(
        self,
        query: str,
        namespace: Optional[str],
    ) -> str:
        """Build a cache key from search parameters."""
        return f"{query}|{namespace or ''}"

    def get(
        self,
        query: str,
        namespace: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Get cached search result if fresh.

        Args:
            query: Search query string
            namespace: Optional namespace filter

        Returns:
            Cached result dict or None if miss/expired
        """
        if self._ttl <= 0:
            return None

        key = self._make_key(query, namespace)
        entry = self._cache.get(key)
        if entry is None:
            return None

        age = time.monotonic() - entry["ts"]
        if age > self._ttl:
            del self._cache[key]
            return None

        return entry["data"]

    def set(
        self,
        query: str,
        namespace: Optional[str],
        data: Dict[str, Any],
    ) -> None:
        """Cache a search result.

        Args:
            query: Search query string
            namespace: Optional namespace filter
            data: The result dict to cache
        """
        if self._ttl <= 0:
            return

        key = self._make_key(query, namespace)
        self._cache[key] = {"data": data, "ts": time.monotonic()}

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()

    @property
    def size(self) -> int:
        """Number of entries currently cached."""
        return len(self._cache)

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "size": len(self._cache),
            "ttl_seconds": self._ttl,
        }


class CapabilityRegistry:
    """Registry for building and managing capability manifests.

    Handles server tools aggregation, namespace inheritance, and manifest caching.
    """

    def __init__(self, query_cache_ttl: int = 300) -> None:
        """Initialize the capability registry.

        Args:
            query_cache_ttl: TTL in seconds for the query result cache (default: 300, 0 disables)
        """
        self._manifest: Dict[str, Any] = {}
        self._namespaces: Dict[str, Any] = {}
        self._groups: Dict[str, Any] = {}
        self._server_tools: Dict[str, List[Dict]] = {}
        self._cache_enabled: bool = True
        self._query_cache: QueryResultCache = QueryResultCache(ttl_seconds=query_cache_ttl)

    def build(self, servers_tools: Dict[str, List]) -> Dict:
        """Build manifest from server tools.

        Args:
            servers_tools: Dict mapping server name to list of tools

        Returns:
            Built manifest dictionary with servers, tools, and metadata
        """
        self._server_tools = servers_tools
        manifest: Dict[str, Any] = {
            "version": "3.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "servers": {},
            "tools_by_server": {},
            "tool_count": 0,
            "server_count": len(servers_tools),
        }

        for server_name, tools in servers_tools.items():
            tool_list = []
            categories: Set[str] = set()

            for tool in tools:
                if not isinstance(tool, dict) or "name" not in tool:
                    logger.warning(f"Invalid tool from {server_name}: {tool}")
                    continue

                tool_entry = {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                }
                tool_list.append(tool_entry)

                category = self._extract_category(tool)
                if category:
                    categories.add(category)

            manifest["servers"][server_name] = {
                "tool_count": len(tool_list),
                "categories": sorted(list(categories)),
                "status": "active",
            }
            manifest["tools_by_server"][server_name] = tool_list
            manifest["tool_count"] += len(tool_list)

        self._manifest = manifest
        self._save_cache()

        logger.info(
            f"Built manifest with {manifest['tool_count']} tools from "
            f"{manifest['server_count']} servers"
        )
        return manifest

    def _extract_category(self, tool: Dict) -> Optional[str]:
        """Extract category from tool name or description.

        Args:
            tool: Tool dictionary

        Returns:
            Category string or None
        """
        name = tool.get("name", "")
        if "__" in name:
            prefix = name.split("__")[0]
            return prefix.replace("_", " ").title()
        return None

    def get_servers(self, namespace: Optional[str] = None) -> List:
        """Get filtered server list based on namespace or group.

        Args:
            namespace: Optional namespace or group name to filter by

        Returns:
            List of server names
        """
        if not self._manifest:
            logger.warning("Manifest not built, returning empty server list")
            return []

        if namespace is None:
            return list(self._manifest.get("servers", {}).keys())

        resolved_servers, error = self.resolve_namespace_to_servers(namespace)
        if error:
            logger.warning(f"Namespace/group resolution failed: {error}")
            return []
        return [s for s in resolved_servers if s in self._manifest.get("servers", {})]

    def get_tools(self, server: str, namespace: Optional[str] = None) -> List:
        """Get filtered tools for a server.

        Args:
            server: Server name to get tools for
            namespace: Optional namespace filter

        Returns:
            List of tools for the server
        """
        if not self._manifest:
            logger.warning("Manifest not built, returning empty tools list")
            return []

        if namespace is not None:
            allowed_servers, error = self.resolve_namespace_to_servers(namespace)
            if error:
                logger.warning(f"Namespace/group resolution failed: {error}")
                return []
            if server not in allowed_servers:
                logger.warning(
                    f"Server '{server}' not in namespace/group '{namespace}'"
                )
                return []

        return self._manifest.get("tools_by_server", {}).get(server, [])

    def validate_inheritance(self, namespaces: Dict) -> List[str]:
        """Validate namespace inheritance for cycles and missing refs.

        Args:
            namespaces: Dict of namespace definitions

        Returns:
            List of warning messages (cycles detected, etc.)
        """
        warnings: List[str] = []
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def detect_cycle(ns_name: str, path: List[str]) -> bool:
            """Detect circular inheritance.

            Args:
                ns_name: Current namespace name
                path: Current path of namespace names

            Returns:
                True if cycle detected
            """
            if ns_name in rec_stack:
                cycle_path = " -> ".join(path + [ns_name])
                warnings.append(f"Circular inheritance detected: {cycle_path}")
                return True

            if ns_name in visited:
                return False

            visited.add(ns_name)
            rec_stack.add(ns_name)

            ns_def = namespaces.get(ns_name)
            if ns_def is None:
                warnings.append(f"Missing namespace reference: '{ns_name}'")
                return False

            extends = self._get_extends(ns_def)
            for ext in extends:
                if ext not in namespaces:
                    raise NamespaceInheritanceError(
                        f"Missing extends reference: '{ext}' in namespace '{ns_name}'"
                    )
                detect_cycle(ext, path + [ns_name])

            rec_stack.remove(ns_name)
            return False

        for ns_name in namespaces:
            if ns_name not in visited:
                detect_cycle(ns_name, [])

        self._namespaces = namespaces
        return warnings

    def _get_extends(self, ns_def: Any) -> List[str]:
        """Get extends list from namespace definition.

        Args:
            ns_def: Namespace definition (list or dict)

        Returns:
            List of extended namespace names
        """
        normalized = normalize_namespace_config(ns_def)
        return normalized["extends"]

    def _get_servers_from_ns(self, ns_def: Any) -> List[str]:
        """Get servers list from namespace definition.

        Args:
            ns_def: Namespace definition (list or dict)

        Returns:
            List of server names
        """
        normalized = normalize_namespace_config(ns_def)
        return normalized["servers"]

    def resolve_namespace(self, namespace: str) -> List[str]:
        """Resolve namespace inheritance to get all accessible servers.

        Args:
            namespace: Namespace name to resolve

        Returns:
            List of all accessible server names (including inherited)

        Raises:
            NamespaceInheritanceError: If namespace not found
        """
        if not self._namespaces:
            self._load_namespaces_from_config()

        if namespace not in self._namespaces:
            raise NamespaceInheritanceError(f"Namespace not found: '{namespace}'")

        resolved: Set[str] = set()
        self._resolve_recursive(namespace, resolved, set())
        return sorted(list(resolved))

    def _resolve_recursive(
        self, namespace: str, resolved: Set[str], visiting: Set[str]
    ) -> None:
        """Recursively resolve namespace inheritance.

        Args:
            namespace: Current namespace to resolve
            resolved: Set of resolved server names
            visiting: Set of currently visiting namespaces (for cycle detection)
        """
        if namespace in visiting:
            logger.warning(f"Skipping circular reference to '{namespace}'")
            return

        ns_def = self._namespaces.get(namespace)
        if ns_def is None:
            logger.warning(f"Namespace '{namespace}' not found during resolution")
            return

        visiting.add(namespace)

        servers = self._get_servers_from_ns(ns_def)
        resolved.update(servers)

        extends = self._get_extends(ns_def)
        for ext in extends:
            self._resolve_recursive(ext, resolved, visiting)

        visiting.remove(namespace)

    def _load_namespaces_from_config(self) -> None:
        """Load namespaces and groups from config file if available."""
        config_path = Path("mcproxy.json")
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
                self._namespaces = config.get("namespaces", {})
                self._groups = config.get("groups", {})
            except Exception as e:
                logger.warning(f"Failed to load namespaces from config: {e}")

    def _save_cache(self) -> None:
        """Save manifest to cache file."""
        if not self._cache_enabled:
            return

        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_data = {
                "manifest": self._manifest,
                "namespaces": self._namespaces,
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(CACHE_FILE, "w") as f:
                json.dump(cache_data, f, indent=2)
            logger.debug(f"Manifest cached to {CACHE_FILE}")
        except Exception as e:
            logger.warning(f"Failed to cache manifest: {e}")

    def load_cache(self) -> Optional[Dict]:
        """Load manifest from cache if fresh.

        Returns:
            Cached manifest or None if not available/stale
        """
        if not self._cache_enabled or not CACHE_FILE.exists():
            return None

        try:
            with open(CACHE_FILE) as f:
                cache_data = json.load(f)

            cached_at_str = cache_data.get("cached_at")
            if cached_at_str:
                cached_at = datetime.fromisoformat(cached_at_str)
                age = (datetime.now(timezone.utc) - cached_at).total_seconds()
                if age > CACHE_TTL_SECONDS:
                    logger.debug("Cache expired, rebuilding")
                    return None

            self._manifest = cache_data.get("manifest", {})
            self._namespaces = cache_data.get("namespaces", {})
            logger.info(f"Loaded manifest from cache ({len(self._manifest)} entries)")
            return self._manifest
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            return None

    @property
    def query_cache(self) -> QueryResultCache:
        """Get the query result cache."""
        return self._query_cache

    def invalidate_cache(self) -> None:
        """Invalidate the manifest cache and query result cache."""
        self._manifest = {}
        self._query_cache.clear()
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
                logger.debug("Cache invalidated")
        except Exception as e:
            logger.warning(f"Failed to invalidate cache: {e}")

    def resolve_group_to_servers(self, group_name: str) -> list[str]:
        """Resolve a group to merged list of server names.

        Args:
            group_name: Name of the group to resolve

        Returns:
            Sorted list of unique server names from all namespaces in the group
        """
        if not self._groups:
            self._load_namespaces_from_config()

        group_def = self._groups.get(group_name)
        if not group_def:
            logger.warning(f"Group '{group_name}' not found")
            return []

        all_servers: set[str] = set()
        ns_refs = group_def.get("namespaces", [])

        for ns_ref in ns_refs:
            actual_name = ns_ref[1:] if ns_ref.startswith("!") else ns_ref
            try:
                servers = self.resolve_namespace(actual_name)
                all_servers.update(servers)
            except NamespaceInheritanceError as e:
                logger.warning(
                    f"Failed to resolve namespace '{actual_name}' in group: {e}"
                )

        return sorted(list(all_servers))

    def get_default_servers(self) -> list[str]:
        """Get servers for unnamespaced /sse endpoint.

        Returns unnamespaced servers plus all non-isolated namespace servers.

        Returns:
            Sorted list of server names accessible via default endpoint
        """
        if not self._namespaces:
            self._load_namespaces_from_config()

        all_servers: set[str] = set()

        for ns_name, ns_def in self._namespaces.items():
            is_isolated = (
                ns_def.get("isolated", False) if isinstance(ns_def, dict) else False
            )
            if not is_isolated:
                try:
                    servers = self.resolve_namespace(ns_name)
                    all_servers.update(servers)
                except NamespaceInheritanceError as e:
                    logger.warning(f"Failed to resolve namespace '{ns_name}': {e}")

        return sorted(list(all_servers))

    def is_namespace_isolated(self, namespace: str) -> bool:
        """Check if namespace requires explicit endpoint.

        Args:
            namespace: Namespace name to check

        Returns:
            True if namespace is isolated, False otherwise
        """
        if not self._namespaces:
            self._load_namespaces_from_config()

        ns_def = self._namespaces.get(namespace)
        if ns_def is None:
            return False

        if isinstance(ns_def, dict):
            return ns_def.get("isolated", False)
        return False

    def resolve_namespace_to_servers(
        self, endpoint_name: Optional[str]
    ) -> tuple[list[str], Optional[str]]:
        """Resolve namespace or group name to servers.

        Args:
            endpoint_name: Namespace name, group name, or None for default

        Returns:
            (servers, error_message) - error_message is None on success
        """
        if not self._namespaces:
            self._load_namespaces_from_config()

        if endpoint_name is None:
            return self.get_default_servers(), None

        if endpoint_name in self._groups:
            servers = self.resolve_group_to_servers(endpoint_name)
            if not servers:
                return [], f"Group '{endpoint_name}' resolved to no servers"
            return servers, None

        if endpoint_name in self._namespaces:
            try:
                servers = self.resolve_namespace(endpoint_name)
                return servers, None
            except NamespaceInheritanceError as e:
                return [], str(e)

        return [], f"Unknown endpoint: '{endpoint_name}'"
