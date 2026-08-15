"""In-memory session variable store with fail-closed semantics.

Sessions are keyed by validated X-Session-ID values bound to an authenticated
principal.  A four-method ``session`` shim (get / set / delete / clear) is
intended to be injected into sandbox exec globals.  All mutations go through
a single internal dict so there are no stale references in user code.

Lifecycle:
    1. Authentication middleware validates credentials and extracts a session
       identifier.
    2. ``bind(session_id, principal)`` is called exactly once per authenticated
       request — it is idempotent so a repeated call with the *same* session_id
       is a no-op, but a call with a *different* session_id raises.
    3. The sandboxed script calls ``session.get(key)``, ``session.set(key,
       value)``, etc.
    4. ``session_stash.discard(session_id)`` (or equivalent expiry hook) removes
       the backing dict; subsequent ``set()`` calls raise
       ``SessionExpiredError`` while other methods silently degrade.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SessionExpiredError(RuntimeError):
    """Raised when ``session.set()`` is called on an expired/absent session."""


class SessionBindingError(RuntimeError):
    """Raised when a second, conflicting session bind is attempted."""


class SessionKeyError(ValueError):
    """Raised when a session variable key fails validation."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum number of concurrent session dicts in the store.
MAX_SESSIONS: int = 10_000

# Maximum number of keys inside any single session dict.
MAX_KEYS_PER_SESSION: int = 256

# Session idle TTL in seconds (0 = disabled).
SESSION_TTL_SECONDS: float = 300.0

# Regex that valid X-Session-ID values must match.
# Adjust to match your session-token format (e.g. UUID, JWT prefix, etc.).
_SESSION_ID_RE: re.Pattern = re.compile(
    r"^[A-Za-z0-9_\-]{16,256}$"
)

# Regex that valid variable *keys* must match.
_SESSION_VAR_KEY_RE: re.Pattern = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$"
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_session_id(session_id: str) -> str:
    """Return *session_id* if it passes format checks, else raise."""
    if not isinstance(session_id, str):
        raise SessionKeyError("session_id must be a string")
    if not _SESSION_ID_RE.match(session_id):
        raise SessionKeyError(
            f"session_id fails format validation: {session_id!r}"
        )
    return session_id


def _validate_var_key(key: str) -> str:
    """Return *key* if it passes format checks, else raise."""
    if not isinstance(key, str):
        raise SessionKeyError("session variable key must be a string")
    if not _SESSION_VAR_KEY_RE.match(key):
        raise SessionKeyError(
            f"session variable key fails format validation: {key!r}"
        )
    return key


# ---------------------------------------------------------------------------
# SessionVariableStore
# ---------------------------------------------------------------------------

class SessionVariableStore:
    """Fail-closed, in-memory store mapping session_id → {var_key: value}.

    Thread-safety is provided by a single ``threading.Lock``.  All public
    methods acquire the lock for the shortest possible duration.
    """

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        max_keys_per_session: int = MAX_KEYS_PER_SESSION,
        ttl_seconds: float = SESSION_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        # session_id → {"_principal": principal, "_ts": float, ...vars}
        self._store: Dict[str, Dict[str, Any]] = {}
        self._max_sessions = max_sessions
        self._max_keys = max_keys_per_session
        self._ttl = ttl_seconds

    # -- internal helpers (MUST be called with self._lock held) -------------

    def _is_expired(self, data: Dict[str, Any]) -> bool:
        if self._ttl <= 0:
            return False
        return (time.monotonic() - data.get("_ts", 0.0)) > self._ttl

    def _evict_expired(self) -> None:
        """Remove all expired sessions.  Caller must hold the lock."""
        if self._ttl <= 0:
            return
        now = time.monotonic()
        expired = [
            sid for sid, data in self._store.items()
            if (now - data.get("_ts", 0.0)) > self._ttl
        ]
        for sid in expired:
            del self._store[sid]

    def _touch(self, data: Dict[str, Any]) -> None:
        """Update the last-access timestamp.  Caller must hold the lock."""
        data["_ts"] = time.monotonic()

    def _var_count(self, data: Dict[str, Any]) -> int:
        """Count user-set keys (exclude internal bookkeeping keys)."""
        return sum(1 for k in data if not k.startswith("_"))

    # -- public lifecycle ---------------------------------------------------

    def bind(self, session_id: str, principal: Any) -> None:
        """Activate a session for the current request context.

        This is **idempotent**: calling ``bind(same_id, same_principal)``
        repeatedly is safe.  Calling with a *different* session_id after a
        prior bind raises ``SessionBindingError`` (fail-closed).

        The call is hooked into the session_stash lifecycle: expired sessions
        are evicted first, and the new (or touched) session dict is created
        under the global cap.
        """
        sid = _validate_session_id(session_id)

        with self._lock:
            self._evict_expired()

            if sid in self._store:
                existing = self._store[sid]
                # Idempotent if same principal — otherwise fail-closed.
                if existing.get("_principal") != principal:
                    raise SessionBindingError(
                        f"Conflicting principal for session {sid!r}"
                    )
                self._touch(existing)
                return

            # Enforce session-count cap.
            if len(self._store) >= self._max_sessions:
                # Try harder: evict expired one more time in case a burst
                # arrived between the first eviction and here.
                self._evict_expired()
                if len(self._store) >= self._max_sessions:
                    raise SessionBindingError(
                        "Session store capacity reached "
                        f"(max_sessions={self._max_sessions})"
                    )

            self._store[sid] = {
                "_principal": principal,
                "_ts": time.monotonic(),
            }

    def discard(self, session_id: str) -> None:
        """Remove a session dict entirely (called by session_stash expiry).

        This is the race-safe expiry hook: after discard, ``set()`` will
        raise ``SessionExpiredError`` while other methods degrade gracefully.
        """
        sid = _validate_session_id(session_id)
        with self._lock:
            self._store.pop(sid, None)

    def discard_expired(self) -> int:
        """Evict all expired sessions and return the count removed."""
        with self._lock:
            before = len(self._store)
            self._evict_expired()
            return before - len(self._store)

    # -- data operations (per-session) -------------------------------------

    def get(self, session_id: str, key: str, default: Any = None) -> Any:
        """Retrieve a session variable.  Degrades gracefully if expired."""
        sid = _validate_session_id(session_id)
        k = _validate_var_key(key)
        with self._lock:
            data = self._store.get(sid)
            if data is None or self._is_expired(data):
                return default
            self._touch(data)
            return data.get(k, default)

    def set(self, session_id: str, key: str, value: Any) -> None:
        """Store a session variable.

        Raises ``SessionExpiredError`` if the session dict is absent (e.g.
        already discarded).  This prevents set-after-discard from leaking
        orphaned dicts.
        """
        sid = _validate_session_id(session_id)
        k = _validate_var_key(key)
        with self._lock:
            data = self._store.get(sid)
            if data is None or self._is_expired(data):
                # Fail-closed: do NOT re-create the dict.
                raise SessionExpiredError(
                    f"Session {sid!r} is expired or does not exist"
                )
            if k not in data and self._var_count(data) >= self._max_keys:
                raise SessionKeyError(
                    f"Session {sid!r} reached key-cap "
                    f"(max_keys_per_session={self._max_keys})"
                )
            data[k] = value
            self._touch(data)

    def delete(self, session_id: str, key: str) -> bool:
        """Remove a session variable.  Degrades gracefully if expired.

        Returns ``True`` if the key existed and was removed, ``False``
        otherwise (including when the session itself is absent).
        """
        sid = _validate_session_id(session_id)
        k = _validate_var_key(key)
        with self._lock:
            data = self._store.get(sid)
            if data is None or self._is_expired(data):
                return False
            removed = data.pop(k, None) is not None
            if removed:
                self._touch(data)
            return removed

    def clear(self, session_id: str) -> None:
        """Remove all user-set variables from a session.

        Degrades gracefully if the session is absent or expired — no error
        is raised.
        """
        sid = _validate_session_id(session_id)
        with self._lock:
            data = self._store.get(sid)
            if data is None or self._is_expired(data):
                return
            # Keep internal bookkeeping keys, drop everything else.
            keys_to_drop = [k for k in data if not k.startswith("_")]
            for k in keys_to_drop:
                del data[k]
            self._touch(data)


# ---------------------------------------------------------------------------
# Session shim (the object injected into sandbox exec globals)
# ---------------------------------------------------------------------------

class SessionShim:
    """Four-method facade bound to a single session for one exec call.

    Only ``get``, ``set``, ``delete``, and ``clear`` are exposed — no access
    to ``bind``/``discard``/store internals.
    """

    __slots__ = ("_store", "_session_id")

    def __init__(self, store: SessionVariableStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    # Deliberately no __getattr__ / __setattr__ — only the four methods.

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(self._session_id, key, default)

    def set(self, key: str, value: Any) -> None:
        self._store.set(self._session_id, key, value)

    def delete(self, key: str) -> bool:
        return self._store.delete(self._session_id, key)

    def clear(self) -> None:
        self._store.clear(self._session_id)


# ---------------------------------------------------------------------------
# Module-level singleton (convenient but optional — tests can instantiate
# their own SessionVariableStore).
# ---------------------------------------------------------------------------

_default_store = SessionVariableStore()


def bind(session_id: str, principal: Any) -> None:
    """Activate *session_id* for *principal* on the default store."""
    _default_store.bind(session_id, principal)


def discard(session_id: str) -> None:
    """Remove *session_id* from the default store (lifecycle hook)."""
    _default_store.discard(session_id)


def make_shim(session_id: str) -> SessionShim:
    """Return a ``SessionShim`` for *session_id* against the default store.

    The returned object is safe to place in sandbox exec globals as
    ``session``.
    """
    return SessionShim(_default_store, session_id)


# ---------------------------------------------------------------------------
# Convenience: build the sandbox globals fragment
# ---------------------------------------------------------------------------

def sandbox_globals(session_id: str) -> Dict[str, Any]:
    """Return ``{"session": <shim>}`` ready to merge into exec globals."""
    return {"session": make_shim(session_id)}
