"""NL orchestrator: intent → match → params with typed errors, one retry,
and a hardened safety gate (conservative advisory classifier, pending-
confirmation store with token_urlsafe(32) IDs, one-shot consume, 60 s TTL,
expiry=deny, auth-context-required).

The orchestrator returns a *neutral* dispatch — no ``nl_origin`` or
``confirmed`` flags.  Those are single-writer internal constructs attached
only at the authenticated NL dispatch site in ``router.py`` via the
private ``_nl_dispatch`` helper.  Client-supplied ``nl_origin`` /
``confirmed`` fields are stripped/rejected at the request boundary.
"""

from __future__ import annotations

import re
import time
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

class NLError(Exception):
    """Base for all NL orchestrator errors."""


class NLMatchError(NLError):
    """No tool could be matched to the intent."""


class NLParamError(NLError):
    """Parameter extraction failed."""


class NLSafetyDenyError(NLError):
    """Advisory classifier denied the parameters (shell-meta default-deny)."""


class NLConfirmError(NLError):
    """Base for confirmation-store errors."""


class NLConfirmExpiredError(NLConfirmError):
    """Confirmation token has expired (TTL elapsed → deny)."""


class NLConfirmConsumeError(NLConfirmError):
    """Confirmation token not found or already consumed (one-shot)."""


class NLConfirmIdentityError(NLConfirmError):
    """Session identity does not match the identity that created the token."""


# ---------------------------------------------------------------------------
# Advisory classifier – conservative, shell-meta default-deny
# ---------------------------------------------------------------------------

_SHELL_META_RE = re.compile(
    r"""[;$`&|<>(){}[\]\\!\n\r'"`]"""
)


class AdvisoryVerdict(Enum):
    SAFE = "safe"
    DENY = "deny"


def classify_advisory(params: Dict[str, Any]) -> AdvisoryVerdict:
    """Return a conservative advisory verdict.

    Any string parameter value containing shell metacharacters results in
    an immediate **DENY**.  This is *advisory* in the sense that callers
    still run the full confirmation gate for sensitive ops, but a DENY
    here is a hard block — the request never proceeds.
    """
    def _check(value: Any) -> AdvisoryVerdict:
        if isinstance(value, str) and _SHELL_META_RE.search(value):
            return AdvisoryVerdict.DENY
        if isinstance(value, dict):
            for v in value.values():
                verdict = _check(v)
                if verdict is AdvisoryVerdict.DENY:
                    return verdict
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                verdict = _check(v)
                if verdict is AdvisoryVerdict.DENY:
                    return verdict
        return AdvisoryVerdict.SAFE

    for key, value in params.items():
        verdict = _check(value)
        if verdict is AdvisoryVerdict.DENY:
            return verdict
    return AdvisoryVerdict.SAFE


# ---------------------------------------------------------------------------
# Pending-confirmation store
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _PendingConfirmation:
    """Internal pending-confirmation record (not exported)."""
    token: str
    session_identity: str
    dispatch: Dict[str, Any]
    created_at: float
    ttl_seconds: float = 60.0

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl_seconds


class PendingConfirmationStore:
    """In-process pending-confirmation store.

    Characteristics
    ---------------
    * Tokens are ``secrets.token_urlsafe(32)`` — 256 bits of entropy.
    * Each token is bound to a **session identity** (authenticated user /
      session id).  Auth context is required for both ``create`` and
      ``consume``.
    * **One-shot consume**: the entry is atomically removed on consume.
    * **60 s TTL**; expiry results in **deny** (never auto-approve).
    """

    def __init__(self) -> None:
        self._store: Dict[str, _PendingConfirmation] = {}

    def create(
        self,
        session_identity: str,
        dispatch: Dict[str, Any],
        ttl_seconds: float = 60.0,
    ) -> str:
        """Stage a pending confirmation and return its token."""
        if not session_identity:
            raise ValueError("session_identity is required")
        token = secrets.token_urlsafe(32)
        self._store[token] = _PendingConfirmation(
            token=token,
            session_identity=session_identity,
            dispatch=dispatch,
            created_at=time.monotonic(),
            ttl_seconds=ttl_seconds,
        )
        return token

    def consume(
        self,
        token: str,
        session_identity: str,
    ) -> Dict[str, Any]:
        """One-shot consume a pending confirmation.

        Returns the stored **neutral** dispatch.

        Raises
        ------
        NLConfirmConsumeError
            Token not found (already consumed or never existed).
        NLConfirmExpiredError
            Token TTL has elapsed — deny.
        NLConfirmIdentityError
            Session identity does not match the creator's identity.
        """
        if not session_identity:
            raise ValueError("session_identity is required")

        entry = self._store.pop(token, None)
        if entry is None:
            raise NLConfirmConsumeError(
                f"Confirmation token not found or already consumed"
            )
        if entry.expired:
            # Expired → deny (never approve)
            raise NLConfirmExpiredError(
                f"Confirmation token expired (TTL {entry.ttl_seconds}s)"
            )
        if not secrets.compare_digest(entry.session_identity, session_identity):
            raise NLConfirmIdentityError(
                "Session identity does not match the identity that created "
                "the confirmation"
            )
        return entry.dispatch

    def purge_expired(self) -> int:
        """Remove all expired entries.  Returns count purged."""
        now = time.monotonic()
        expired = [
            tok for tok, entry in self._store.items()
            if (now - entry.created_at) > entry.ttl_seconds
        ]
        for tok in expired:
            del self._store[tok]
        return len(expired)


# Module-level singleton — shared across the NL dispatch path.
pending_confirmations = PendingConfirmationStore()


# ---------------------------------------------------------------------------
# Orchestrator data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolMatch:
    """Result of matching an NL intent to a tool."""
    tool_name: str
    confidence: float
    raw_params: Dict[str, Any] = ()
    __hash__ = None  # type: ignore[assignment]  # mutable default guard

    def __post_init__(self) -> None:
        if not isinstance(self.raw_params, dict):
            object.__setattr__(self, "raw_params", {})


@dataclass(frozen=True, slots=True)
class NLResult:
    """Neutral dispatch result from the orchestrator.

    **No ``nl_origin`` or ``confirmed`` flags are present.**  Those are
    single-writer internal constructs attached only by the authenticated
    NL dispatch helper (``_nl_dispatch`` in ``router.py``).  The
    orchestrator itself returns a clean, policy-neutral dispatch so that
    internal callers receive unmarked, status-quo-policy dispatches with
    no bypass.
    """
    dispatch: Dict[str, Any]
    needs_confirmation: bool = False
    confirmation_token: Optional[str] = None
    match: Optional[ToolMatch] = None


# Type aliases for the pluggable matcher / param-extractor callables.
MatcherFn = Callable[[Any, str], Optional[ToolMatch]]
ParamExtractorFn = Callable[[Any, str, ToolMatch], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class NLOrchestrator:
    """Intent → match → params pipeline with one retry and safety gate.

    The returned ``NLResult.dispatch`` is **always** a neutral dict
    (``{"tool": ..., "params": ...}``) with no NL-origin markers.
    """

    MAX_RETRIES: int = 1

    def __init__(
        self,
        *,
        matcher: MatcherFn,
        param_extractor: ParamExtractorFn,
    ) -> None:
        self._matcher = matcher
        self._param_extractor = param_extractor

    # -- public API ---------------------------------------------------------

    def orchestrate(
        self,
        intent: str,
        tool_registry: Any,
        session_identity: str,
    ) -> NLResult:
        """Run the full intent → match → params pipeline.

        Parameters
        ----------
        intent : str
            The raw natural-language intent text.
        tool_registry
            The application tool registry (must expose ``get_schema(name)``).
        session_identity : str
            Authenticated session identity (required; empty/falsy raises).

        Returns
        -------
        NLResult
            Neutral dispatch — no ``nl_origin`` / ``confirmed`` flags.

        Raises
        ------
        NLSafetyDenyError
            Advisory classifier denies the extracted parameters.
        NLMatchError
            No tool match after retry.
        NLParamError
            Parameter extraction failed after retry.
        ValueError
            ``session_identity`` is empty.
        """
        if not session_identity:
            raise ValueError("session_identity is required for NL orchestration")

        last_error: Optional[Exception] = None
        for _attempt_idx in range(1 + self.MAX_RETRIES):
            try:
                return self._attempt(intent, tool_registry, session_identity)
            except (NLMatchError, NLParamError) as exc:
                last_error = exc
                continue

        # All retries exhausted — fail closed.
        raise last_error  # type: ignore[misc]

    # -- internals ----------------------------------------------------------

    def _attempt(
        self,
        intent: str,
        tool_registry: Any,
        session_identity: str,
    ) -> NLResult:
        # 1. Match intent to a tool
        match = self._matcher(tool_registry, intent)
        if match is None or not match.tool_name:
            raise NLMatchError(f"No tool match for intent: {intent!r}")

        # 2. Extract parameters
        try:
            schema = tool_registry.get_schema(match.tool_name)
            params = self._param_extractor(schema, intent, match)
        except NLError:
            raise
        except Exception as exc:
            raise NLParamError(
                f"Parameter extraction failed for {match.tool_name!r}: {exc}"
            ) from exc

        # 3. Safety gate — advisory classifier (shell-meta default-deny)
        verdict = classify_advisory(params)
        if verdict is AdvisoryVerdict.DENY:
            raise NLSafetyDenyError(
                "Shell metacharacter detected in extracted parameters — denied"
            )

        # 4. Build neutral dispatch (NO nl_origin, NO confirmed)
        dispatch: Dict[str, Any] = {
            "tool": match.tool_name,
            "params": params,
        }

        # 5. If the tool requires confirmation, stage a pending
        #    confirmation instead of returning a ready-to-execute dispatch.
        needs_confirmation = self._requires_confirmation(
            match.tool_name, tool_registry
        )
        confirmation_token: Optional[str] = None
        if needs_confirmation:
            confirmation_token = pending_confirmations.create(
                session_identity=session_identity,
                dispatch=dispatch,
            )

        return NLResult(
            dispatch=dispatch,
            needs_confirmation=needs_confirmation,
            confirmation_token=confirmation_token,
            match=match,
        )

    @staticmethod
    def _requires_confirmation(tool_name: str, tool_registry: Any) -> bool:
        """Determine whether the tool demands confirmation.

        Fails **closed** on ambiguity: if the schema is missing or the
        flag cannot be determined, confirmation is required.
        """
        try:
            schema = tool_registry.get_schema(tool_name)
        except Exception:
            return True  # fail closed
        if schema is None:
            return True  # fail closed
        return bool(getattr(schema, "requires_confirmation", True))
