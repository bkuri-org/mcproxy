"""Per-tool health tracking with 24h rolling window."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Rolling window duration in seconds (24 hours)
_WINDOW_SECONDS = 24 * 60 * 60

# Maximum length for redacted error strings
_MAX_ERROR_LENGTH = 200


@dataclass
class _CallRecord:
    """Single recorded tool invocation."""

    ts: float  # monotonic timestamp
    latency: float  # seconds
    success: bool
    caller: str
    error: Optional[str]  # pre-redacted / truncated


def _redact_error(error: Optional[str]) -> Optional[str]:
    """Redact and truncate an error string at record time.

    Once stored, the error is safe to expose under authz without
    further processing.
    """
    if error is None:
        return None
    if len(error) > _MAX_ERROR_LENGTH:
        return error[:_MAX_ERROR_LENGTH] + "\u2026"
    return error


class HealthTracker:
    """Records per-tool latency/outcomes in a 24h rolling window.

    Uses internal monotonic timestamps so ``record()`` takes no caller
    time.  Errors are redacted/truncated at record time so the stored
    data is safe to return from ``get_metrics()`` under the existing
    inspect authz/scope check.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, List[_CallRecord]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        tool: str,
        caller: str,
        latency: float,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Record a tool invocation outcome.

        Parameters
        ----------
        tool:
            Tool name / identifier.
        caller:
            Caller identity (e.g. session id or user id) used to
            enforce *min_distinct_callers* before marking unhealthy.
        latency:
            Execution latency in seconds (measured by the caller).
        success:
            Whether the call succeeded.
        error:
            Optional error string – **redacted/truncated here** so the
            stored version is safe to expose later.
        """
        rec = _CallRecord(
            ts=time.monotonic(),
            latency=latency,
            success=success,
            caller=caller,
            error=_redact_error(error),
        )
        with self._lock:
            self._records[tool].append(rec)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self, records: List[_CallRecord], now: float) -> List[_CallRecord]:
        """Drop entries older than the rolling window.

        Records are appended in strictly increasing monotonic order,
        so we can scan from the front.
        """
        cutoff = now - _WINDOW_SECONDS
        for i, r in enumerate(records):
            if r.ts >= cutoff:
                return records[i:]
        return []  # all expired

    def _windowed(self, tool: str) -> Tuple[List[_CallRecord], float]:
        """Return ``(pruned_records, now)`` for *tool*."""
        now = time.monotonic()
        with self._lock:
            raw = self._records.get(tool)
            if not raw:
                return [], now
            pruned = self._prune(raw, now)
            # Write back the pruned list to prevent unbounded growth.
            self._records[tool] = pruned
            return pruned, now

    # ------------------------------------------------------------------
    # Public queries – used by router.py
    # ------------------------------------------------------------------

    def is_unhealthy(
        self,
        tool: str,
        threshold: float,
        min_samples: int,
        min_distinct_callers: int,
    ) -> Optional[Tuple[str, str]]:
        """Check whether *tool* is considered unhealthy.

        Returns ``(tool, reason)`` when unhealthy, ``None`` otherwise.

        A tool is flagged unhealthy when **all** of:
        * success rate < *threshold* (0–1)
        * total samples >= *min_samples*
        * distinct callers >= *min_distinct_callers*

        The *min_distinct_callers* guard ensures a single session
        cannot globally disable a tool.
        """
        records, _ = self._windowed(tool)
        total = len(records)
        if total == 0:
            return None

        if total < min_samples:
            return None

        distinct_callers = len({r.caller for r in records})
        if distinct_callers < min_distinct_callers:
            return None

        successes = sum(1 for r in records if r.success)
        rate = successes / total
        if rate < threshold:
            reason = (
                f"success rate {rate:.1%} below threshold {threshold:.1%} "
                f"({successes}/{total} samples, {distinct_callers} distinct callers)"
            )
            return (tool, reason)

        return None

    def check(self, tool: str) -> Optional[str]:
        """Reason string when *tool* is unhealthy, ``None`` otherwise.

        ponytail: record() is never called yet, so this always returns None
        until call outcomes get wired in.
        """
        result = self.is_unhealthy(
            tool, threshold=0.5, min_samples=20, min_distinct_callers=2
        )
        return result[1] if result else None

    # ------------------------------------------------------------------
    # Public queries – used by inspect.py
    # ------------------------------------------------------------------

    def get_metrics(self, tool: str) -> Optional[dict]:
        """Return aggregated metrics for *tool*, or ``None`` if no data.

        The returned dict contains **only pre-redacted errors** and is
        safe to expose under the existing inspect authz/scope check
        (the caller must already pass that gate before calling this).
        """
        records, _ = self._windowed(tool)
        if not records:
            return None

        total = len(records)
        successes = sum(1 for r in records if r.success)
        latencies = sorted(r.latency for r in records)
        errors = [
            r.error for r in records if not r.success and r.error is not None
        ]
        distinct_callers = len({r.caller for r in records})

        avg_lat = sum(latencies) / len(latencies)
        p50 = latencies[len(latencies) // 2]
        p99_idx = min(int(len(latencies) * 0.99), len(latencies) - 1)
        p99 = latencies[p99_idx]

        return {
            "tool": tool,
            "total_calls": total,
            "successes": successes,
            "failures": total - successes,
            "success_rate": successes / total,
            "distinct_callers": distinct_callers,
            "latency_avg": avg_lat,
            "latency_p50": p50,
            "latency_p99": p99,
            "latency_max": latencies[-1],
            "errors": errors,  # already redacted/truncated at record time
        }

    def get_all_tools(self) -> List[str]:
        """Return tool names that have any recorded data (may be stale)."""
        with self._lock:
            return list(self._records.keys())


# Module-level singleton
health_tracker = HealthTracker()
