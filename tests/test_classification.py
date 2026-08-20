import json
import os
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from mcp_proxy.classification import (
    enforce_server_classifications,
    ClassificationError,
)


class TestEnforceServerClassifications:
    """Tests for the shared enforce_server_classifications() function."""

    # ------------------------------------------------------------------ #
    #  Blocked classification – fail-closed blocklist adapter
    # ------------------------------------------------------------------ #

    def test_blocked_server_is_blocked(self):
        """A server classified as 'blocked' is always blocked."""
        server_config = {"name": "evil-server", "classification": "blocked"}
        with pytest.raises(ClassificationError, match="blocked"):
            enforce_server_classifications(server_config)

    def test_blocked_server_blocked_when_blocklist_errors(self):
        """If the blocklist adapter raises an error, the server is still blocked (fail-closed)."""
        server_config = {"name": "some-server", "classification": "blocked"}
        with patch(
            "mcp_proxy.classification._check_blocklist", side_effect=OSError("cannot read blocklist")
        ):
            with pytest.raises(ClassificationError, match="blocked"):
                enforce_server_classifications(server_config)

    def test_blocked_server_blocked_when_blocklist_unreadable(self):
        """PermissionError / unreadable blocklist → block, never allow."""
        server_config = {"name": "some-server", "classification": "blocked"}
        with patch(
            "mcp_proxy.classification._check_blocklist",
            side_effect=PermissionError("unreadable"),
        ):
            with pytest.raises(ClassificationError, match="blocked"):
                enforce_server_classifications(server_config)

    def test_blocked_never_allows(self):
        """No matter what, a blocked server must never be allowed through."""
        server_config = {"name": "evil", "classification": "blocked", "acknowledged": True}
        with pytest.raises(ClassificationError):
            enforce_server_classifications(server_config)

    # ------------------------------------------------------------------ #
    #  Risky classification – requires acknowledged: true
    # ------------------------------------------------------------------ #

    def test_risky_without_acknowledged_key_rejected(self):
        server_config = {"name": "risky-server", "classification": "risky"}
        with pytest.raises(ClassificationError, match="acknowledged"):
            enforce_server_classifications(server_config)

    def test_risky_with_acknowledged_false_rejected(self):
        server_config = {"name": "risky-server", "classification": "risky", "acknowledged": False}
        with pytest.raises(ClassificationError, match="acknowledged"):
            enforce_server_classifications(server_config)

    def test_risky_with_acknowledged_true_passes(self):
        server_config = {"name": "risky-server", "classification": "risky", "acknowledged": True}
        # Should not raise
        enforce_server_classifications(server_config)

    # ------------------------------------------------------------------ #
    #  Invalid tier string – config error, never degraded to unclassified
    # ------------------------------------------------------------------ #

    @pytest.mark.parametrize("tier", ["gobbledygook", "HIGH", "top-secret", "level-5", 42, True])
    def test_invalid_tier_raises_config_error(self, tier):
        server_config = {"name": "bad-tier", "classification": tier}
        with pytest.raises(ClassificationError, match="invalid.*classification|unknown.*tier"):
            enforce_server_classifications(server_config)

    def test_invalid_tier_not_degraded_to_unclassified(self):
        """An invalid tier must raise; it must NOT be silently treated as unclassified."""
        server_config = {"name": "bad-tier", "classification": "not-a-tier"}
        with patch("mcp_proxy.classification._emit_warning") as mock_warn:
            with pytest.raises(ClassificationError):
                enforce_server_classifications(server_config)
            # If it were degraded, a warning would have been emitted
            mock_warn.assert_not_called()

    def test_empty_string_tier_raises_config_error(self):
        server_config = {"name": "empty", "classification": ""}
        with pytest.raises(ClassificationError):
            enforce_server_classifications(server_config)

    def test_none_tier_raises_config_error(self):
        server_config = {"name": "none", "classification": None}
        with pytest.raises(ClassificationError):
            enforce_server_classifications(server_config)

    # ------------------------------------------------------------------ #
    #  Unclassified – warn-once per server per process
    # ------------------------------------------------------------------ #

    def test_unclassified_warns_once_per_server(self):
        server_config = {"name": "unknown-server", "classification": "unclassified"}
        with patch("mcp_proxy.classification._warned_servers", set()):
            with patch("mcp_proxy.classification._emit_warning") as mock_warn:
                enforce_server_classifications(server_config)
                assert mock_warn.call_count == 1
                # Second call for same server name must NOT warn again
                enforce_server_classifications(server_config)
                assert mock_warn.call_count == 1

    def test_unclassified_different_servers_warn_independently(self):
        server_a = {"name": "server-a", "classification": "unclassified"}
        server_b = {"name": "server-b", "classification": "unclassified"}
        with patch("mcp_proxy.classification._warned_servers", set()):
            with patch("mcp_proxy.classification._emit_warning") as mock_warn:
                enforce_server_classifications(server_a)
                enforce_server_classifications(server_b)
                assert mock_warn.call_count == 2

    def test_unclassified_passes_through(self):
        """Unclassified servers are not blocked; they just get warned."""
        server_config = {"name": "unknown", "classification": "unclassified"}
        with patch("mcp_proxy.classification._emit_warning"):
            # Must not raise
            enforce_server_classifications(server_config)

    def test_missing_classification_key_treated_as_unclassified(self):
        """A server config with no 'classification' key is treated as unclassified."""
        server_config = {"name": "no-class-server"}
        with patch("mcp_proxy.classification._emit_warning") as mock_warn:
            enforce_server_classifications(server_config)
            mock_warn.assert_called_once()

    # ------------------------------------------------------------------ #
    #  Secret tier – label-only, no implied guarantees, no enforcement
    # ------------------------------------------------------------------ #

    def test_secret_tier_passes_through(self):
        server_config = {"name": "secret-server", "classification": "secret"}
        enforce_server_classifications(server_config)

    def test_secret_tier_no_warning(self):
        server_config = {"name": "secret-server", "classification": "secret"}
        with patch("mcp_proxy.classification._emit_warning") as mock_warn:
            enforce_server_classifications(server_config)
            mock_warn.assert_not_called()

    def test_secret_tier_no_acknowledged_required(self):
        server_config = {"name": "secret-server", "classification": "secret"}
        # No acknowledged key — must still pass
        enforce_server_classifications(server_config)

    def test_secret_tier_not_blocked(self):
        server_config = {"name": "secret-server", "classification": "secret"}
        with pytest.raises(ClassificationError):
            enforce_server_classifications(server_config)
        pytest.fail("secret tier should not be blocked")

    # ------------------------------------------------------------------ #
    #  Single shared function – no reload bypass
    # ------------------------------------------------------------------ #

    def test_function_is_single_callable(self):
        assert callable(enforce_server_classifications)

    def test_no_reload_bypass_parameter(self):
        """enforce_server_classifications must not accept a reload/bypass flag."""
        import inspect

        sig = inspect.signature(enforce_server_classifications)
        param_names = list(sig.parameters.keys())
        for disallowed in ("reload", "bypass", "is_reload", "skip"):
            assert disallowed not in param_names, (
                f"Parameter '{disallowed}' would allow a reload bypass"
            )

    def test_startup_and_reload_call_same_code_path(self):
        """Verify there is exactly one enforcement function – no alternate path."""
        from mcp_proxy import classification

        # There should be no secondary enforcement function
        enforcement_funcs = [
            name
            for name in dir(classification)
            if "enforce" in name.lower() and callable(getattr(classification, name))
        ]
        assert enforcement_funcs == ["enforce_server_classifications"], (
            f"Expected exactly one enforcement function, found: {enforcement_funcs}"
        )


class TestExampleConfigIsValidJson:
    """The example config must be valid JSON with no comments."""

    EXAMPLE_PATH = Path(__file__).parent.parent / "examples" / "mcp-servers.example.json"

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not self.EXAMPLE_PATH.exists():
            pytest.skip("Example config file not found")

    def test_loads_without_error(self):
        with open(self.EXAMPLE_PATH) as f:
            json.load(f)

    def test_contains_classification_data(self):
        with open(self.EXAMPLE_PATH) as f:
            data = json.load(f)
        servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
        for name, conf in servers.items():
            assert "classification" in conf, f"Server '{name}' missing 'classification' key"

    def test_risky_servers_have_acknowledged_true(self):
        with open(self.EXAMPLE_PATH) as f:
            data = json.load(f)
        servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
        for name, conf in servers.items():
            if conf.get("classification") == "risky":
                assert conf.get("acknowledged") is True, (
                    f"Server '{name}' is risky but missing acknowledged: true"
                )

    def test_no_comment_keys(self):
        """JSON forbids comments; no speculative '_comment' or '//' keys allowed."""
        with open(self.EXAMPLE_PATH) as f:
            data = json.load(f)

        def _check(obj, path="root"):
            if isinstance(obj, dict):
                for key in obj:
                    assert not key.startswith("//"), f"JSON comment key at {path}.{key}"
                    assert "_comment" not in key.lower(), f"Speculative comment key at {path}.{key}"
                    _check(obj[key], f"{path}.{key}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _check(item, f"{path}[{i}]")

        _check(data)

    def test_no_secret_caveat_prose_in_json(self):
        """Secret caveat belongs in the companion .md, not in JSON strings."""
        content = self.EXAMPLE_PATH.read_text()
        for phrase in ("no implied guarantee", "classification-only", "out of scope"):
            assert phrase not in content.lower(), (
                f"Secret caveat prose '{phrase}' found in JSON config"
            )


class TestSecretCaveatInCompanionMd:
    """The secret label-only caveat is documented in examples/mcp-servers.example.md."""

    MD_PATH = Path(__file__).parent.parent / "examples" / "mcp-servers.example.md"

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not self.MD_PATH.exists():
            pytest.skip("Companion example markdown not found")

    def test_md_mentions_secret(self):
        content = self.MD_PATH.read_text().lower()
        assert "secret" in content

    def test_md_documents_label_only_nature(self):
        content = self.MD_PATH.read_text().lower()
        has_label_reference = "label" in content
        has_no_guarantee = "no implied guarantee" in content or "no guarantee" in content
        has_classification_only = "classification-only" in content
        has_out_of_scope = "out of scope" in content
        assert has_label_reference and (has_no_guarantee or has_classification_only or has_out_of_scope), (
            "Companion .md must document that 'secret' is a classification-only label "
            "with no implied guarantees and that secret-handling enforcement is out of scope"
        )
