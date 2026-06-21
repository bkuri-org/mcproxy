"""Tests for sandbox namespace access control and configuration."""

from sandbox import (
    AccessControlConfig,
    NamespaceAccessControl,
)


class TestNamespaceAccessControl:
    """Tests for NamespaceAccessControl."""

    def test_can_access_allowed(self, namespace_access_control: NamespaceAccessControl):
        allowed, error = namespace_access_control.can_access("browser", "playwright")

        assert allowed is True
        assert error == ""

    def test_can_access_denied(self, namespace_access_control: NamespaceAccessControl):
        allowed, error = namespace_access_control.can_access("browser", "filesystem")

        assert allowed is False
        assert "does not have access" in error

    def test_can_access_namespace_not_found(
        self, namespace_access_control: NamespaceAccessControl
    ):
        allowed, error = namespace_access_control.can_access(
            "nonexistent", "playwright"
        )

        assert allowed is False
        assert "not found" in error

    def test_can_access_inheritance(
        self, namespace_access_control: NamespaceAccessControl
    ):
        allowed, error = namespace_access_control.can_access("privileged", "playwright")

        assert allowed is True

        allowed, error = namespace_access_control.can_access("privileged", "filesystem")

        assert allowed is True

        allowed, error = namespace_access_control.can_access("privileged", "system")

        assert allowed is True

    def test_can_access_crypto_namespace(
        self, namespace_access_control: NamespaceAccessControl
    ):
        allowed, _ = namespace_access_control.can_access("security", "crypto")
        assert allowed is True

        allowed, _ = namespace_access_control.can_access("security", "playwright")
        assert allowed is False

    def test_get_allowed_tools(self, namespace_access_control: NamespaceAccessControl):
        tools, error = namespace_access_control.get_allowed_tools(
            "browser", "playwright"
        )

        assert error == ""
        assert "playwright__navigate" in tools
        assert "playwright__click" in tools

    def test_get_allowed_tools_denied(
        self, namespace_access_control: NamespaceAccessControl
    ):
        tools, error = namespace_access_control.get_allowed_tools(
            "browser", "filesystem"
        )

        assert tools == []
        assert "does not have access" in error

    def test_resolve_allowed_servers_circular(
        self, namespace_access_control: NamespaceAccessControl
    ):
        servers = namespace_access_control._resolve_allowed_servers("circular_a")

        assert "playwright" in servers or "filesystem" in servers


class TestAccessControlConfig:
    """Tests for AccessControlConfig dataclass."""

    def test_get_server(self, sandbox_manifest: AccessControlConfig):
        server = sandbox_manifest.get_server("playwright")

        assert server is not None
        assert "tools" in server

    def test_get_server_not_found(self, sandbox_manifest: AccessControlConfig):
        server = sandbox_manifest.get_server("nonexistent")

        assert server is None

    def test_get_namespace(self, sandbox_manifest: AccessControlConfig):
        ns = sandbox_manifest.get_namespace("browser")

        assert ns is not None

    def test_get_namespace_not_found(self, sandbox_manifest: AccessControlConfig):
        ns = sandbox_manifest.get_namespace("nonexistent")

        assert ns is None

    def test_get_tools_for_server(self, sandbox_manifest: AccessControlConfig):
        tools = sandbox_manifest.get_tools_for_server("playwright")

        assert len(tools) == 3
        assert "playwright__navigate" in tools

    def test_get_tools_for_server_not_found(
        self, sandbox_manifest: AccessControlConfig
    ):
        tools = sandbox_manifest.get_tools_for_server("nonexistent")

        assert tools == []
