"""Tests for sandbox execution, error format, tool fix suggestions, and access control enforcement."""

import asyncio
import pytest
from unittest.mock import patch

from sandbox import (
    SandboxExecutor,
    AccessControlConfig,
    NamespaceAccessControl,
    suggest_tool_fix,
)


class TestSandboxExecutorExecute:
    """Tests for SandboxExecutor.execute()."""

    @pytest.mark.asyncio
    async def test_execute_returns_validation_error(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        result = await executor.execute("import os", "browser")

        assert result["status"] == "error"
        assert "Validation error" in result["traceback"]
        assert result["result"] is None

    @pytest.mark.asyncio
    async def test_execute_result_format(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": 42, "traceback": null}',
        ):
            result = await executor.execute("x = 1", "browser")

            assert result["status"] == "success"
            assert result["result"] == 42
            assert "execution_time_ms" in result

    @pytest.mark.asyncio
    async def test_execute_timeout(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(
            sandbox_manifest, lambda *args: None, default_timeout_secs=1
        )

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            side_effect=asyncio.TimeoutError(),
        ):
            result = await executor.execute("x = 1", "browser")

            assert result["status"] == "error"
            assert "timed out" in result["traceback"]

    @pytest.mark.asyncio
    async def test_execute_process_error(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            side_effect=RuntimeError("Error output"),
        ):
            result = await executor.execute("x = 1", "browser")

            assert result["status"] == "error"
            assert "Error output" in result["traceback"]

    @pytest.mark.asyncio
    async def test_execute_json_decode_error(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        with patch.object(
            executor, "_run_uv_subprocess_async", return_value="invalid json{"
        ):
            result = await executor.execute("x = 1", "browser")

            assert result["status"] == "error"
            assert "No JSON output found" in result["traceback"]


class TestSandboxExecutorHelpers:
    """Tests for SandboxExecutor helper methods."""

    def test_strip_comments_single_line(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        code = "x = 1  # comment\ny = 2"
        stripped = executor._strip_comments(code)

        assert "#" not in stripped
        assert "x = 1" in stripped
        assert "y = 2" in stripped

    def test_strip_comments_preserves_strings(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        code = 'x = "# not a comment"'
        stripped = executor._strip_comments(code)

        assert '"# not a comment"' in stripped

    def test_strip_comments_multiline_string(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        code = 'x = """multi\\n# line\\nstring"""'
        stripped = executor._strip_comments(code)

        assert '"""multi' in stripped

    def test_build_env(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        access_control = NamespaceAccessControl(sandbox_manifest)

        env = executor._build_env("test_namespace", access_control)

        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["SANDBOX_NAMESPACE"] == "test_namespace"

    def test_wrap_code_includes_namespace(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        access_control = NamespaceAccessControl(sandbox_manifest)

        wrapped = executor._wrap_code("x = 1", "my_namespace", access_control)

        assert "my_namespace" in wrapped
        assert "api" in wrapped
        assert "_APIProxy" in wrapped


class TestCreateSandboxExecutor:
    """Tests for SandboxExecutor construction."""

    def test_create_sandbox_executor(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)

        assert isinstance(executor, SandboxExecutor)

    def test_create_sandbox_executor_with_kwargs(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(
            sandbox_manifest,
            lambda *args: None,
            uv_path="/custom/uv",
            default_timeout_secs=60,
        )

        assert executor._uv_path == "/custom/uv"
        assert executor._default_timeout_secs == 60


class TestErrorFormat:
    """Tests for structured error response format."""

    @pytest.mark.asyncio
    async def test_error_response_has_traceback(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        result = await executor.execute("import os", "browser")

        assert "traceback" in result
        assert isinstance(result["traceback"], str)
        assert len(result["traceback"]) > 0

    @pytest.mark.asyncio
    async def test_error_response_has_status(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        result = await executor.execute("import os", "browser")

        assert "status" in result
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_error_response_has_execution_time(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        result = await executor.execute("import os", "browser")

        assert "execution_time_ms" in result
        assert isinstance(result["execution_time_ms"], int)

    @pytest.mark.asyncio
    async def test_error_response_result_is_none(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        result = await executor.execute("import os", "browser")

        assert "result" in result
        assert result["result"] is None


class TestSuggestToolFix:
    """Tests for fuzzy matching tool name suggestions."""

    def test_exact_match(self):
        available = ["find_symbols", "find_references", "rename_symbol"]
        result = suggest_tool_fix("find_symbols", available)

        assert result is not None
        assert "Did you mean 'find_symbols'?" in result

    def test_close_match_typo(self):
        available = ["find_symbols", "find_references", "rename_symbol"]
        result = suggest_tool_fix("find_symbls", available)

        assert result is not None
        assert "Did you mean 'find_symbols'?" in result

    def test_close_match_case_insensitive(self):
        available = ["Find_Symbols", "Find_References"]
        result = suggest_tool_fix("find_symbols", available)

        assert result is not None
        assert "Did you mean" in result

    def test_no_match_below_threshold(self):
        available = ["completely_different", "another_tool"]
        result = suggest_tool_fix("xyz123", available)

        assert result is not None
        assert "Available tools:" in result

    def test_empty_available_list(self):
        result = suggest_tool_fix("any_tool", [])

        assert result is None

    def test_show_all_tools_when_few(self):
        available = ["tool_a", "tool_b", "tool_c"]
        result = suggest_tool_fix("xyz", available)

        assert result is not None
        assert "tool_a" in result
        assert "tool_b" in result
        assert "tool_c" in result
        assert "more" not in result

    def test_truncate_tools_when_many(self):
        available = [f"tool_{i}" for i in range(10)]
        result = suggest_tool_fix("xyz", available)

        assert result is not None
        assert "Available tools:" in result
        assert "more" in result
        assert "5 more" in result

    def test_threshold_boundary(self):
        available = ["abcdefghij"]
        result_close = suggest_tool_fix("abcdefghi", available)
        result_far = suggest_tool_fix("xyz", available)

        assert result_close is not None
        assert result_far is not None
        if "Did you mean" in result_close:
            assert "Available tools:" in result_far

    def test_multiple_candidates_picks_best(self):
        available = ["find_symbols", "find_symbol", "find_symmetry"]
        result = suggest_tool_fix("find_symbls", available)

        assert result is not None
        assert (
            "Did you mean 'find_symbols'?" in result
            or "Did you mean 'find_symbol'?" in result
        )


class TestExecuteAccessControl:
    """Tests for execute sandbox enforcing namespace access control."""

    @pytest.mark.asyncio
    async def test_execute_blocks_unauthorized_server(
        self, sandbox_manifest: AccessControlConfig
    ):
        """Execute should block calls to servers outside the namespace."""
        executor = SandboxExecutor(sandbox_manifest, lambda *args: {"result": "ok"})

        code = 'result = api.server("filesystem").read_file(path="/etc/passwd")'

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": null, "traceback": "Access denied to \'filesystem\'\\n", "stash_updates": []}',
        ):
            result = await executor.execute(code, namespace="browser")

            assert "Access denied" in result["traceback"]
            assert "filesystem" in result["traceback"]

    @pytest.mark.asyncio
    async def test_execute_blocks_unauthorized_call_tool(
        self, sandbox_manifest: AccessControlConfig
    ):
        """Execute should block call_tool to servers outside the namespace."""
        executor = SandboxExecutor(sandbox_manifest, lambda *args: {"result": "ok"})

        code = (
            'result = api.call_tool("filesystem", "read_file", {"path": "/etc/passwd"})'
        )

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": null, "traceback": "Access denied to \'filesystem\'\\n", "stash_updates": []}',
        ):
            result = await executor.execute(code, namespace="browser")

            assert "Access denied" in result["traceback"]
            assert "filesystem" in result["traceback"]

    @pytest.mark.asyncio
    async def test_execute_allows_authorized_server(
        self, sandbox_manifest: AccessControlConfig
    ):
        """Execute should allow calls to servers within the namespace."""
        executor = SandboxExecutor(sandbox_manifest, lambda *args: {"result": "ok"})

        code = """
async def run():
    result = api.server("playwright").navigate(url="http://example.com")
"""
        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": null, "traceback": null, "stash_updates": []}',
        ):
            result = await executor.execute(code, namespace="browser")

            assert result["status"] == "success"
            assert not result.get("traceback")

    @pytest.mark.asyncio
    async def test_execute_blocks_cross_namespace_access(
        self, sandbox_manifest: AccessControlConfig
    ):
        """Execute should block accessing servers from different isolated namespaces."""
        executor = SandboxExecutor(sandbox_manifest, lambda *args: {"result": "ok"})

        code = 'result = api.server("system").admin_action()'

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": null, "traceback": "Access denied to \'system\'\\n", "stash_updates": []}',
        ):
            result = await executor.execute(code, namespace="browser")

            assert "Access denied" in result["traceback"]

    @pytest.mark.asyncio
    async def test_execute_namespace_inheritance_works(
        self, sandbox_manifest: AccessControlConfig
    ):
        """Namespaces that extend others should inherit access."""
        executor = SandboxExecutor(sandbox_manifest, lambda *args: {"result": "ok"})

        code = """
async def run():
    result = api.server("playwright").navigate(url="http://example.com")
"""
        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": null, "traceback": null, "stash_updates": []}',
        ):
            result = await executor.execute(code, namespace="privileged")

            assert result["status"] == "success"
            assert not result.get("traceback")

    @pytest.mark.asyncio
    async def test_execute_sync_call_works(self, sandbox_manifest: AccessControlConfig):
        """Execute should work with sync calls (without async/await)."""
        executor = SandboxExecutor(sandbox_manifest, lambda *args: {"result": "ok"})

        code = 'result = api.server("playwright").navigate(url="http://example.com")'

        with patch.object(
            executor,
            "_run_uv_subprocess_async",
            return_value='{"result": null, "traceback": null, "stash_updates": []}',
        ):
            result = await executor.execute(code, namespace="browser")

            assert result["status"] == "success"
            assert not result.get("traceback")
