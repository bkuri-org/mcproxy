"""Async token-bucket rate limiter for tool calls.

Internal unit: tokens/sec.  Dual-bucket (tool + namespace) with atomic
no-await acquire so timed-out waits never consume tokens.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Name normalisation – fail-closed import guard
# ---------------------------------------------------------------------------

try:
    from utils.namespace import normalize_tool_name as _normalize_tool_name
except Exception as _exc:
    raise RuntimeError(
        "rate_limit: failed to import normalize_tool_name from "
        "utils.namespace – refusing to operate without name "
        "normalisation (fail-closed)"
    ) from _exc


def normalize_tool_name(name: str) -> str:
    """Public facade – delegates to shared implementation."""
    return _normalize_tool_name(name)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BucketConfig:
    """Per-bucket configuration in unified tokens/sec."""

    rate: float
    burst: float

    @classmethod
    def from_per_minute(
        cls,
        max_calls_per_minute: float,
        max_burst: Optional[float] = None,
    ) -> "BucketConfig":
        rate = max_calls_per_minute / 60.0
        burst = (
            max_burst
            if max_burst is not None
            else max(1.0, math.ceil(rate))
        )
        return cls(rate=rate, burst=burst)

    @classmethod
    def from_per_second(
        cls,
        max_calls_per_second: float,
        max_burst: Optional[float] = None,
    ) -> "BucketConfig":
        rate = max_calls_per_second
        burst = (
            max_burst
            if max_burst is not None
            else max(1.0, math.ceil(rate))
        )
        return cls(rate=rate, burst=burst)


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Minimal token bucket.

    peek()       – async wait until >=1 token available, no consumption.
    try_acquire() – sync atomic consume-if-available.
    release()     – sync refund (cancellation path only).
    """

    __slots__ = ("_rate", "_burst", "_tokens", "_last", "_lock")

    def __init__(self, rate: float, burst: float) -> None:
        if rate <= 0:
            raise RuntimeError(f"rate must be > 0, got {rate}")
        if burst <= 0:
            raise RuntimeError(f"burst must be > 0, got {burst}")
        self._rate = rate
        self._burst = burst
        self._tokens: float = burst
        self._last: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        if dt > 0:
            self._tokens = min(self._burst, self._tokens + dt * self._rate)
            self._last = now

    async def peek(self, timeout: float) -> bool:
        """Wait (non-consuming) for >=1 token.  Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            self._refill()
            if self._tokens >= 1.0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait = min(remaining, (1.0 - self._tokens) / self._rate)
            await asyncio.sleep(wait)

    def try_acquire(self) -> bool:
        """Sync, no-await atomic consume-if-available."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def release(self) -> None:
        """Sync, no-await refund.  Safe only before dispatch."""
        self._tokens = min(self._burst, self._tokens + 1.0)

    def snapshot(self) -> dict:
        """Current state for metadata (tool bucket only, on success)."""
        self._refill()
        return {
            "limit": self._burst,
            "remaining": max(0, math.floor(self._tokens)),
            "reset_seconds": (
                max(0.0, (self._burst - self._tokens) / self._rate)
                if self._rate > 0
                else 0.0
            ),
        }


# ---------------------------------------------------------------------------
# Mixed-unit guard (module-level, fail-closed)
# ---------------------------------------------------------------------------

_REGISTERED_UNIT: Optional[str] = None  # "per_second" | "per_minute"


def _check_unit(unit: str) -> None:
    global _REGISTERED_UNIT
    if _REGISTERED_UNIT is None:
        _REGISTERED_UNIT = unit
    elif _REGISTERED_UNIT != unit:
        raise RuntimeError(
            f"rate_limit: mixed unit types not supported – "
            f"'{_REGISTERED_UNIT}' already registered, "
            f"rejecting '{unit}'"
        )


def _reset_unit_tracking() -> None:
    """For testing only."""
    global _REGISTERED_UNIT
    _REGISTERED_UNIT = None


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """Dual-bucket rate limiter (tool + namespace).

    Any configured limit forces single-worker mode unless
    ``allow_multi_worker=True`` is explicitly set.
    """

    max_wait_seconds: float = 30.0
    allow_multi_worker: bool = False

    _tool_buckets: Dict[str, _TokenBucket] = field(
        default_factory=dict, init=False, repr=False,
    )
    _ns_buckets: Dict[str, _TokenBucket] = field(
        default_factory=dict, init=False, repr=False,
    )
    _has_limits: bool = field(default=False, init=False, repr=False)

    # -- registration ---------------------------------------------------------

    def register_tool_limit(
        self,
        tool_name: str,
        *,
        per_second: Optional[float] = None,
        per_minute: Optional[float] = None,
        max_burst: Optional[float] = None,
    ) -> None:
        name = normalize_tool_name(tool_name)
        cfg = self._make_config(per_second, per_minute, max_burst)
        self._tool_buckets[name] = _TokenBucket(cfg.rate, cfg.burst)
        self._has_limits = True

    def register_namespace_limit(
        self,
        namespace: str,
        *,
        per_second: Optional[float] = None,
        per_minute: Optional[float] = None,
        max_burst: Optional[float] = None,
    ) -> None:
        cfg = self._make_config(per_second, per_minute, max_burst)
        self._ns_buckets[namespace] = _TokenBucket(cfg.rate, cfg.burst)
        self._has_limits = True

    @staticmethod
    def _make_config(
        per_second: Optional[float],
        per_minute: Optional[float],
        max_burst: Optional[float],
    ) -> BucketConfig:
        if per_second is not None and per_minute is not None:
            raise RuntimeError(
                "rate_limit: specify per_second OR per_minute, not both"
            )
        if per_second is not None:
            _check_unit("per_second")
            return BucketConfig.from_per_second(per_second, max_burst)
        if per_minute is not None:
            _check_unit("per_minute")
            return BucketConfig.from_per_minute(per_minute, max_burst)
        raise RuntimeError(
            "rate_limit: per_second or per_minute required"
        )

    # -- properties -----------------------------------------------------------

    @property
    def requires_single_worker(self) -> bool:
        """Fail-closed: any limit => single worker unless opted out."""
        return self._has_limits and not self.allow_multi_worker

    # -- acquire / release ----------------------------------------------------

    async def acquire(
        self,
        tool_name: str,
        namespace: str,
    ) -> Tuple[bool, Optional[dict]]:
        """Acquire permission for a tool+namespace pair.

        Returns ``(allowed, quota_meta)`` where *quota_meta* is populated
        only on success and only for the tool bucket (caller's own
        invoked-tool bucket).

        Protocol
        --------
        1. Non-consuming ``peek()`` on every relevant bucket (bounded by
           ``max_wait_seconds`` total budget across attempts).
        2. Exactly one atomic no-await dual ``try_acquire()`` on both
           buckets – zero awaits between consume and the caller's
           dispatch.
        3. On timeout or interrupted wait: zero consumption, both
           buckets fully restored.
        4. Retry: at most one retry with <=2 s backoff (deducted from
           the time budget).  Retry cannot double-consume because each
           attempt performs its own fresh peek+try_acquire cycle.
        """
        tool_key = normalize_tool_name(tool_name)
        tb = self._tool_buckets.get(tool_key)
        nb = self._ns_buckets.get(namespace)

        # Fast path – no limits for this pair
        if tb is None and nb is None:
            return True, None

        deadline = time.monotonic() + self.max_wait_seconds

        for attempt in range(2):  # 0 = initial, 1 = single retry
            # Backoff before retry (capped at 2 s, budget-aware)
            if attempt == 1:
                backoff = min(2.0, deadline - time.monotonic())
                if backoff <= 0:
                    return False, None
                await asyncio.sleep(backoff)

            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return False, None

            # --- Step 1: non-consuming peek on all relevant buckets ---
            ok = True
            if tb is not None:
                ok = ok and await tb.peek(remaining)
            if nb is not None and ok:
                ok = ok and await nb.peek(remaining)

            if not ok:
                # Timeout – zero consumption, both buckets untouched
                continue

            # --- Step 2: atomic no-await dual try_acquire ---
            got_t = tb.try_acquire() if tb is not None else True
            got_n = nb.try_acquire() if nb is not None else True

            if got_t and got_n:
                # Success – quota meta for the tool bucket only
                meta = tb.snapshot() if tb is not None else None
                return True, meta

            # Defensive rollback (should not happen after successful
            # peek, but protects against races in multi-worker mode)
            if got_t and tb is not None:
                tb.release()
            if got_n and nb is not None:
                nb.release()

        # All attempts exhausted – caller should raise generic 429
        return False, None

    def release(self, tool_name: str, namespace: str) -> None:
        """No-await refund – call only if cancelled *before* dispatch.

        After the request is issued the token is considered spent;
        calling release() post-dispatch would over-credit the bucket.
        """
        tool_key = normalize_tool_name(tool_name)
        tb = self._tool_buckets.get(tool_key)
        nb = self._ns_buckets.get(namespace)
        if tb is not None:
            tb.release()
        if nb is not None:
            nb.release()


# ---------------------------------------------------------------------------
# HTTP header helper
# ---------------------------------------------------------------------------

def make_ratelimit_headers(
    meta: Optional[dict],
    *,
    authenticated: bool,
) -> Optional[dict]:
    """Emit ``X-RateLimit-*`` headers **only** when all of:

    * ``meta`` is not ``None`` (success path for the invoked tool),
    * the transport is plain-HTTP (caller decides to call this),
    * the response passed the **same** auth as the tool call
      (``authenticated=True``).

    Returns ``None`` when headers must be omitted (unauthenticated
    transports, failures, missing metadata).
    """
    if not authenticated or meta is None:
        return None
    return {
        "X-RateLimit-Limit": str(meta["limit"]),
        "X-RateLimit-Remaining": str(meta["remaining"]),
        "X-RateLimit-Reset": str(meta["reset_seconds"]),
    }
