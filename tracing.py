"""Trace store for admin routes.

# ponytail: stub — admin_routes imports TraceStore but nothing ever constructs
# one or sets app.state.trace_store, so trace endpoints 503 by design until a
# real tracing backend exists.
"""

from typing import Any, Dict, List, Optional


class TraceStore:
    """In-memory trace records (never wired; admin routes 503 without it)."""

    def __init__(self, max_traces: int = 1000) -> None:
        self._traces: List[Dict[str, Any]] = []

    def record(self, trace: Dict[str, Any]) -> None:
        self._traces.append(trace)
        if len(self._traces) > 1000:
            self._traces = self._traces[-1000:]

    def get(self, trace_id: str) -> Optional[Dict[str, Any]]:
        for t in self._traces:
            if t.get("id") == trace_id:
                return t
        return None

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        return list(reversed(self._traces[-limit:]))
