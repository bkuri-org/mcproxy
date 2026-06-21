"""Tests for sandbox code validation: blocked imports, blocked builtins, and validation logic."""

import pytest

from sandbox import (
    SandboxExecutor,
    AccessControlConfig,
    MAX_CODE_SIZE_BYTES,
)


class TestSandboxExecutorValidation:
    """Tests for SandboxExecutor.validate_code()."""

    def test_validate_code_valid(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "x = 1 + 2\nresult = x * 3"
        is_valid, error = executor.validate_code(code)

        assert is_valid is True
        assert error == ""

    def test_validate_code_syntax_error(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "def broken(\n  pass"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "Syntax error" in error

    def test_validate_code_size_limit(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        large_code = "x = 1\n" * (MAX_CODE_SIZE_BYTES // 4)
        is_valid, error = executor.validate_code(large_code)

        assert is_valid is False
        assert "exceeds maximum size" in error

    def test_validate_code_size_exactly_at_limit(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code_size = MAX_CODE_SIZE_BYTES - 100
        code = "x = 1\n" * (code_size // 6)
        is_valid, error = executor.validate_code(code)

        assert is_valid is True

    def test_validate_code_unicode_normalization(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "x = '\uff41'"  # Full-width 'a'
        is_valid, error = executor.validate_code(code)

        assert is_valid is True


class TestBlockedImports:
    """Tests for blocked import detection."""

    def test_blocked_import_os(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import os"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "Blocked import detected" in error
        assert "os" in error

    def test_blocked_import_sys(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import sys"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "sys" in error

    def test_blocked_import_subprocess(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import subprocess"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "subprocess" in error

    def test_blocked_import_socket(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import socket"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "socket" in error

    def test_blocked_import_http(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import http.client"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "http" in error

    def test_blocked_import_urllib(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "from urllib.request import urlopen"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "urllib" in error

    def test_blocked_import_requests(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import requests"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "requests" in error

    def test_blocked_import_shutil(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import shutil"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "shutil" in error

    def test_blocked_import_tempfile(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import tempfile"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "tempfile" in error

    def test_blocked_import_multiprocessing(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import multiprocessing"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "multiprocessing" in error

    def test_blocked_import_from_syntax(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "from os import path"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "os" in error

    def test_allowed_import(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "import json\nimport math"
        is_valid, error = executor.validate_code(code)

        assert is_valid is True

    def test_blocked_import_in_comment_ignored(
        self, sandbox_manifest: AccessControlConfig
    ):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "# import os\nimport json"
        is_valid, error = executor.validate_code(code)

        assert is_valid is True


class TestBlockedBuiltins:
    """Tests for blocked builtin detection."""

    def test_blocked_builtin_eval(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "x = eval('1+1')"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "eval" in error

    def test_blocked_builtin_exec(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "exec('x = 1')"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "exec" in error

    def test_blocked_builtin_compile(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "compile('x = 1', '<string>', 'exec')"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "compile" in error

    def test_blocked_builtin_open(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "f = open('file.txt')"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "open" in error

    def test_blocked_builtin_input(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "x = input('prompt')"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "input" in error

    def test_blocked_builtin_breakpoint(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "breakpoint()"
        is_valid, error = executor.validate_code(code)

        assert is_valid is False
        assert "breakpoint" in error

    def test_allowed_builtin_call(self, sandbox_manifest: AccessControlConfig):
        executor = SandboxExecutor(sandbox_manifest, lambda *args: None)
        code = "x = len([1, 2, 3])\ny = str(x)"
        is_valid, error = executor.validate_code(code)

        assert is_valid is True
