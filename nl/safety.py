"""NL safety gate: advisory classification, pending-confirmation store,
and helpers for identity-verified confirm/deny flow.

Design invariants
-----------------
* ``nl_origin`` and ``confirmed`` are **single-writer internal constructs**
  attached exclusively by the unexported ``_nl_dispatch`` helper inside
  ``router.py`` at the authenticated NL dispatch site.
* The orchestrator returns a *neutral* dispatch carrying **no NL flags**.
  Internal callers receive an unmarked, status-quo-policy dispatch — there
  is no bypass.
* ``confirmed`` derives **solely** from identity-verified one-shot consume
  of a pending-confirmation token.
* Client-supplied ``nl_origin`` / ``confirmed`` fields are **stripped and
  rejected** at the request boundary (see :func:`strip_nl_fields`).
* The ``execute.py`` gate is scoped to NL-originated calls only; direct
  (non-NL) tool invocations are unaffected.
* All NL-dispatched execution is routed through the existing sandbox runner
  with **no bypass**, failing closed on ambiguity.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from secrets import token_urlsafe
from typing import Any, Dict


# ── Typed errors ──────────────────────────────────────────────────────────


class NLStatus(str, Enum):
    """Status codes surfaced by the NL pipeline."""

    OK = "ok"
    CLASSIFY_DENIED = "classify_denied"
    CONFIRM_REQUIRED = "confirm_required"
    CONFIRM_EXPIRED = "confirm_expired"
    CONFIRM_MISMATCH = "confirm_mismatch"
    CONFIRM_ALREADY_CONSUMED = "confirm_already_consumed"
    AUTH_MISSING = "auth_missing"
    AMBIGUOUS = "ambiguous"


class NLError(Exception):
    """Base exception for the NL safety gate."""

    def __init__(self, message: str, status: NLStatus) -> None:
        super().__init__(message)
        self.status = status


class ClassificationError(NLError):
    """The classifier encountered an internal error (fail-closed)."""

    def __init__(self, message: str = "classification error") -> None:
        super().__init__(message, NLStatus.AMBIGUOUS)


class ClassificationDenied(NLError):
    """The classifier determined the call is not safe to auto-execute."""

    def __init__(self, message: str = "classification denied") -> None:
        super().__init__(message, NLStatus.CLASSIFY_DENIED)


class ConfirmationRequired(NLError):
    """The call requires explicit user confirmation before execution."""

    def __init__(self, token: str, message: str = "confirmation required") -> None:
        super().__init__(message, NLStatus.CONFIRM_REQUIRED)
        self.token = token


class ConfirmationExpired(NLError):
    """The pending-confirmation token has expired (TTL elapsed → deny)."""

    def __init__(self, token: str, message: str = "confirmation expired") -> None:
        super().__init__(message, NLStatus.CONFIRM_EXPIRED)
        self.token = token


class ConfirmationMismatch(NLError):
    """The token does not belong to the requesting session."""

    def __init__(self, message: str = "confirmation session mismatch") -> None:
        super().__init__(message, NLStatus.CONFIRM_MISMATCH)


class ConfirmationConsumed(NLError):
    """The token was already consumed (one-shot)."""

    def __init__(self, message: str = "confirmation already consumed") -> None:
        super().__init__(message, NLStatus.CONFIRM_ALREADY_CONSUMED)


class AuthContextMissing(NLError):
    """An NL safety operation was attempted without an auth context."""

    def __init__(self, message: str = "auth context required") -> None:
        super().__init__(message, NLStatus.AUTH_MISSING)


# ── Advisory verdict ──────────────────────────────────────────────────────


class AdvisoryVerdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


# ── Advisory classifier ───────────────────────────────────────────────────

# Shell meta characters — any occurrence in a string parameter triggers
# default-deny because we cannot statically determine shell expansion
# behaviour inside sandboxed execution.
_SHELL_META_RE = re.compile(r'[`$\\;|&<>(){}\[\]\'"!\n\r\t]')

# Tools that are inherently advisory-unsafe regardless of parameters.
_ALWAYS_DENY_TOOLS: frozenset[str] = frozenset({
    # Extend as needed — empty by default; the shell-meta check is
    # the primary conservative gate.
})

# Tools that are known-safe and skip further checks.
_ALWAYS_ALLOW_TOOLS: frozenset[str] = frozenset({
    # Extend with tool names that are read-only / side-effect-free.
})


def classify_advisory(
    tool_name: str,
    params: Dict[str, Any],
    *,
    strict: bool = True,
) -> AdvisoryVerdict:
    """Conservative advisory classifier.

    Rules (evaluated in order, first match wins):

    1. Tool in ``_ALWAYS_DENY_TOOLS`` → DENY.
    2. Tool in ``_ALWAYS_ALLOW_TOOLS`` → ALLOW.
    3. Any string value containing shell-meta characters → DENY.
    4. If *strict* is ``False`` and no deny triggered → ALLOW.
    5. Otherwise (strict mode, no explicit allow) → CONFIRM.

    On any unexpected error the function raises :class:`ClassificationError`
    (fail-closed / deny-by-default on ambiguity).
    """
    try:
        if tool_name in _ALWAYS_DENY_TOOLS:
            return AdvisoryVerdict.DENY

        if tool_name in _ALWAYS_ALLOW_TOOLS:
            return AdvisoryVerdict.ALLOW

        # Recursively scan all string values for shell meta characters.
        if _contains_shell_meta(params):
            return AdvisoryVerdict.DENY

        if not strict:
            return AdvisoryVerdict.ALLOW

        # Conservative default: require confirmation when strict.
        return AdvisoryVerdict.CONFIRM

    except NLError:
        raise
    except Exception as exc:
        # Fail closed — never allow on classifier error.
        raise ClassificationError(
            f"classification error for {tool_name!r}: {exc}"
        ) from exc


def _contains_shell_meta(value: Any) -> bool:
    """Return ``True`` if *value* or any nested string contains shell meta."""
    if isinstance(value, str):
        return bool(_SHELL_META_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_shell_meta(v) for v in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_shell_meta(v) for v in value)
    return False


# ── Pending confirmation store ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """Immutable record of a pending user-confirmation."""

    token: str
    session_id: str
    tool_name: str
    params: Dict[str, Any]
    created_at: float
    ttl: float = 60.0

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class PendingConfirmationStore:
    """In-process pending-confirmation store.

    Invariants:
    * IDs are ``token_urlsafe(32)`` (256 bits of entropy).
    * Each token is bound to a *session_id* (identity).
    * Tokens are **one-shot**: the first ``consume`` removes them.
    * TTL is 60 seconds; expiry results in **deny**.
    * All mutating operations require a non-empty *session_id* (auth
      context check).
    * Thread-safe via a single lock.
    """

    _DEFAULT_TTL = 60.0

    def __init__(self, *, ttl: float = _DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._store: Dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────

    def create(
        self,
        *,
        session_id: str,
        tool_name: str,
        params: Dict[str, Any],
    ) -> str:
        """Create a new pending confirmation and return its token.

        Raises :class:`AuthContextMissing` if *session_id* is empty.
        """
        _require_session(session_id)
        token = token_urlsafe(32)
        now = time.monotonic()
        record = PendingConfirmation(
            token=token,
            session_id=session_id,
            tool_name=tool_name,
            params=dict(params),
            created_at=now,
            ttl=self._ttl,
        )
        with self._lock:
            self._store[token] = record
            self._cleanup_expired()
        return token

    def consume(
        self,
        *,
        token: str,
        session_id: str,
    ) -> PendingConfirmation:
        """Atomically consume (remove and return) a pending confirmation.

        Raises:
            :class:`AuthContextMissing` — no session.
            :class:`ConfirmationMismatch` — token belongs to another session.
            :class:`ConfirmationExpired` — TTL elapsed (deny).
            :class:`ConfirmationConsumed` — token not found (one-shot).
        """
        _require_session(session_id)
        with self._lock:
            record = self._store.pop(token, None)
            if record is None:
                # Could be expired-and-cleaned or already consumed.
                raise ConfirmationConsumed()
            if record.session_id != session_id:
                # Re-insert so the rightful owner can still consume.
                self._store[token] = record
                raise ConfirmationMismatch()
            if record.is_expired:
                # Expired → deny (do NOT re-insert).
                raise ConfirmationExpired(token)
            return record

    def deny(
        self,
        *,
        token: str,
        session_id: str,
    ) -> None:
        """Explicitly deny a pending confirmation (user rejected it).

        Raises :class:`AuthContextMissing` if *session_id* is empty.
        Silently no-ops if the token does not exist or belongs to another
        session (the token will expire naturally).
        """
        _require_session(session_id)
        with self._lock:
            record = self._store.pop(token, None)
            if record is not None and record.session_id != session_id:
                # Not ours — put it back.
                self._store[token] = record

    # ── internal ──────────────────────────────────────────────────────

    def _cleanup_expired(self) -> None:
        """Remove expired entries.  Must be called under ``_lock``."""
        now = time.monotonic()
        expired = [
            tok
            for tok, rec in self._store.items()
            if now > rec.expires_at
        ]
        for tok in expired:
            del self._store[tok]


# ── Request boundary helpers ──────────────────────────────────────────────

_NL_INTERNAL_FIELDS = frozenset({"nl_origin", "confirmed"})


def strip_nl_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any client-supplied ``nl_origin`` / ``confirmed`` fields.

    These are **single-writer internal constructs** set exclusively by the
    authenticated ``_nl_dispatch`` helper in ``router.py``.  Client-supplied
    values are silently removed so they can never influence policy or bypass
    the safety gate.
    """
    cleaned: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in _NL_INTERNAL_FIELDS:
            continue
        cleaned[key] = value
    return cleaned


def require_auth_context(auth_context: Any) -> Any:
    """Return *auth_context* if truthy; otherwise raise
    :class:`AuthContextMissing`."""
    if not auth_context:
        raise AuthContextMissing()
    return auth_context


def _require_session(session_id: str) -> None:
    """Internal fast-path session check."""
    if not session_id:
        raise AuthContextMissing("session_id is required")


# ── Module-level singleton (shared across the process) ────────────────────

default_store = PendingConfirmationStore()
"""Process-wide pending-confirmation store.

Import and use this singleton unless you have a specific reason to create
a separate instance (e.g. testing).
"""
