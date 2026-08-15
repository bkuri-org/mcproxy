"""
MockEngine – intercepts tool execution, returns shape-safe mock responses,
and optionally records real responses to mock/recordings/.

Security:
  • Explicit opt-in only (MOCK_ENABLED env var must be "1" or "true")
  • Filenames are sanitised to an allowlist (alphanumeric, dash, underscore, dot)
  • Recordings are written atomically via write-to-tmpfile + os.replace
  • File permissions are forced to 0o600
  • Secret scrubbing removes common sensitive keys before writing
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MOCK_ENABLED = os.getenv("MOCK_ENABLED", "").lower() in ("1", "true")

# Directory relative to this file's location
_RECORDINGS_DIR = Path(__file__).resolve().parent / "mock" / "recordings"

# Keys whose *values* are scrubbed from recordings
_SECRET_KEY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"api_key",
        r"apikey",
        r"secret",
        r"token",
        r"password",
        r"passwd",
        r"authorization",
        r"bearer",
        r"cookie",
        r"session[_-]?id",
        r"private[_-]?key",
    )
]

# Allowlist for filename characters
_FILENAME_ALLOWLIST = re.compile(r"^[a-zA-Z0-9_\-.]+$")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_writelock = Lock()  # protects concurrent atomic writes


def _is_enabled() -> bool:
    """Return True only when explicitly opted-in via MOCK_ENABLED."""
    return _MOCK_ENABLED


def _scrub_secrets(obj: Any) -> Any:
    """Recursively replace values of secret-looking keys with '[SCRUBBED]'."""
    if isinstance(obj, dict):
        return {
            k: "[SCRUBBED]"
            if any(p.search(k) for p in _SECRET_KEY_PATTERNS)
            else _scrub_secrets(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_secrets(item) for item in obj]
    return obj


def _safe_filename(tool_name: str, suffix: str = "") -> str:
    """Produce a sanitised, allowlisted filename.

    Strips any character not in [a-zA-Z0-9_-.] and collapses runs.
    """
    raw = f"{tool_name}{suffix}"
    sanitized = re.sub(r"[^a-zA-Z0-9_\-.]", "_", raw)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if not sanitized:
        sanitized = "_unnamed"
    if not _FILENAME_ALLOWLIST.match(sanitized):
        # Should never happen after the above, but guard anyway
        raise ValueError(f"Filename sanitisation failed: {sanitized!r}")
    return sanitized


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically with 0o600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _annotate_mocked(response: Any) -> dict[str, Any]:
    """Ensure a response is a dict with ``mocked: True``.

    Non-dict responses are wrapped so callers always receive a consistent
    shape.
    """
    if isinstance(response, dict):
        out = dict(response)
        out["mocked"] = True
        return out
    return {"mocked": True, "value": response}


# ---------------------------------------------------------------------------
# MockEngine
# ---------------------------------------------------------------------------


class MockEngine:
    """Intercept layer for tool execution.

    Usage from ``server/handlers/tools/execute.py``::

        from mock import get_mock_engine

        mock_engine = get_mock_engine()
        # ...
        if mock_engine.should_mock(tool_name, request_payload):
            return mock_engine.mock_response(tool_name, request_payload)
        real = actual_tool_execute(...)
        mock_engine.record(tool_name, request_payload, real)
        return real

    Wired in ``api_parallel.py`` by importing the same singleton and calling
    ``set_request_mock`` before dispatching when the request carries mock
    overrides.

    The engine is **inactive** unless ``MOCK_ENABLED=1`` is set in the
    environment.
    """

    def __init__(
        self,
        *,
        config_mocks: Optional[Dict[str, Any]] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        """
        Parameters
        ----------
        config_mocks:
            Optional mapping of ``tool_name -> mock_value`` loaded from a
            config file or environment.  These act as *static* mocks.
        enabled:
            Override the global ``MOCK_ENABLED`` flag (useful for testing).
        """
        self._config_mocks: Dict[str, Any] = config_mocks or {}
        self._enabled: bool = enabled if enabled is not None else _is_enabled()
        # Per-request overrides: tool_name -> mock_value
        self._request_mocks: Dict[str, Any] = {}
        # Recording toggle – off by default; enable via enable_recording()
        self._recording: bool = False

    # -- public api ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def should_mock(self, tool_name: str, request_payload: Any) -> bool:
        """Decide whether to return a mock for this call."""
        if not self._enabled:
            return False
        # Request-level mock takes precedence
        if tool_name in self._request_mocks:
            return True
        # Config-level mock
        if tool_name in self._config_mocks:
            return True
        return False

    def mock_response(self, tool_name: str, request_payload: Any) -> dict[str, Any]:
        """Return a shape-safe mock response annotated with ``mocked: True``.

        Priority: request-level > config-level > generic fallback.
        """
        if tool_name in self._request_mocks:
            raw = self._request_mocks[tool_name]
        elif tool_name in self._config_mocks:
            raw = self._config_mocks[tool_name]
        else:
            raw = {"error": "mock response not configured", "tool": tool_name}
        return _annotate_mocked(raw)

    def set_request_mock(self, tool_name: str, value: Any) -> None:
        """Register a per-request mock override.

        Called from ``api_parallel.py`` when the incoming request specifies
        mock data for a given tool.
        """
        self._request_mocks[tool_name] = value

    def clear_request_mocks(self) -> None:
        """Remove all per-request overrides."""
        self._request_mocks.clear()

    # -- recording ----------------------------------------------------------

    def enable_recording(self) -> None:
        """Turn on response recording (opt-in, off by default).

        Raises ``RuntimeError`` if the engine itself is not enabled.
        """
        if not self._enabled:
            raise RuntimeError("Cannot enable recording when mock engine is disabled")
        self._recording = True

    @property
    def recording(self) -> bool:
        return self._recording

    def record(
        self,
        tool_name: str,
        request_payload: Any,
        response: Any,
    ) -> None:
        """Persist a scrubbed copy of the real response to ``mock/recordings/``.

        The write is atomic (tmpfile + rename) and the file is created with
        mode 0o600 (owner-read/write only).  Secrets are scrubbed before
        serialisation.
        """
        if not self._recording:
            return

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        suffix = f"_{timestamp}"
        filename = _safe_filename(tool_name, suffix=suffix) + ".json"
        dest = _RECORDINGS_DIR / filename

        entry = {
            "tool": tool_name,
            "timestamp": timestamp,
            "request": _scrub_secrets(request_payload),
            "response": _scrub_secrets(response),
        }

        with _writelock:
            _atomic_write_json(dest, entry)


# ---------------------------------------------------------------------------
# Module-level singleton (convenient for simple wiring)
# ---------------------------------------------------------------------------

_singleton: Optional[MockEngine] = None
_singleton_lock = Lock()


def get_mock_engine() -> MockEngine:
    """Return the module-level MockEngine singleton.

    Both ``execute.py`` and ``api_parallel.py`` should import this function
    to share the same engine instance.
    """
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = MockEngine()
    return _singleton
