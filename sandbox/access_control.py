"""Access control for sandbox execution."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class AccessControlConfig:
    """Simplified manifest view for sandbox access control."""

    servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    namespaces: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    groups: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def get_server(self, name: str) -> Optional[Dict[str, Any]]:
        return self.servers.get(name)

    def get_namespace(self, name: str) -> Optional[Dict[str, Any]]:
        return self.namespaces.get(name)

    def get_group(self, name: str) -> Optional[Dict[str, Any]]:
        return self.groups.get(name)

    def get_tools_for_server(self, server_name: str) -> List[str]:
        server = self.get_server(server_name)
        if not server:
            return []
        return server.get("tools", [])


@dataclass
class NamespaceAccessControl:
    """Controls access to servers based on namespace permissions."""

    manifest: "AccessControlConfig"

    def can_access(self, namespace: str, target_server: str) -> Tuple[bool, str]:
        """Check if namespace can access target server.

        Args:
            namespace: The namespace requesting access
            target_server: The server being accessed

        Returns:
            Tuple of (allowed: bool, error_message: str)
        """
        # Fail closed: no namespace = no tool access. Pure code still runs;
        # only api.server(...) calls hit this check.
        if not namespace:
            available = ", ".join(sorted(self.manifest.namespaces.keys())) or "none"
            return False, (
                "No namespace specified. Pass namespace= in the dispatch call "
                f"or an X-Namespace header. Available: {available}"
            )

        ns_config = self.manifest.get_namespace(namespace)
        if not ns_config:
            return False, f"Namespace '{namespace}' not found in manifest"

        allowed_servers = self._resolve_allowed_servers(namespace)

        if target_server in allowed_servers:
            return True, ""

        return False, (
            f"Namespace '{namespace}' does not have access to server '{target_server}'. "
            f"Allowed servers: {', '.join(sorted(allowed_servers)) or 'none'}"
        )

    def _resolve_allowed_servers(self, namespace: str) -> Set[str]:
        """Resolve all allowed servers including from inheritance.

        Args:
            namespace: The namespace to resolve

        Returns:
            Set of allowed server names
        """
        resolved: Set[str] = set()
        visited: Set[str] = set()

        def _resolve(ns: str) -> None:
            if ns in visited:
                return
            visited.add(ns)

            ns_config = self.manifest.get_namespace(ns)
            if not ns_config:
                return

            resolved.update(ns_config.get("servers", []))

            for parent in ns_config.get("extends", []):
                _resolve(parent)

        _resolve(namespace)
        return resolved

    def get_allowed_tools(
        self, namespace: str, server_name: str
    ) -> Tuple[List[str], str]:
        """Get list of tools namespace can use on a server.

        Args:
            namespace: The namespace requesting access
            server_name: The server being accessed

        Returns:
            Tuple of (tools: List[str], error_message: str)
        """
        can_access, error = self.can_access(namespace, server_name)
        if not can_access:
            return [], error

        return self.manifest.get_tools_for_server(server_name), ""
