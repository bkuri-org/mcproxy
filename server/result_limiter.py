"""Result limiter enforcing a cumulative byte budget over nested structures.

Public API
----------
apply_result_limit(result, max_bytes) -> result

The returned object is structurally identical but guaranteed to serialise
within *max_bytes* (UTF-8 / JSON approximation).  Budget exhaustion is the
sole governor; per-item caps merely prevent any single collection element
from swallowing the entire remaining budget.

Wiring notes (implemented at call-sites, not here):
* ``execute.py`` calls ``apply_result_limit`` after every MCP tool return.
* ``http_backend.py`` / ``api_parallel.py`` call it at their response-
  assembly points if they bypass ``execute.py`` — all via this single module.
* ``max_result_size`` is extracted (and removed) from the tool-call params
  before forwarding, clamped to ``cache.max_result_size_bytes`` (default
  50 000) from the config file.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["apply_result_limit"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRUNCATION_MARKER = "…"
_BINARY_PLACEHOLDER = "<binary data, {size} bytes>"
_BASE64_PLACEHOLDER = "<base64 data, {length} chars>"
_JSON_SUMMARY_PREFIX = "<JSON: "
_JSON_SUMMARY_SUFFIX = ">"
_LOG_SUMMARY_FIRST_LINES = 5
_LOG_SUMMARY_LAST_LINES = 2
_BASE64_MIN_LENGTH = 100
_PER_ITEM_BUDGET_FRACTION = 0.10
_PER_ITEM_MIN_BYTES = 64
_STRUCTURAL_OVERHEAD_BRACKETS = 2        # [] or {}
_STRUCTURAL_OVERHEAD_PER_LIST_ITEM = 2   # comma + space
_STRUCTURAL_OVERHEAD_PER_DICT_ENTRY = 4  # comma + space after value
_SMALL_SCALAR_COST = 8
_MAX_DEPTH = 64

# Regex: lines that look like log entries (date/time/level prefix)
_LOG_LINE_RE = re.compile(
    r"^(?:"
    r"\d{4}[-/]\d{2}[-/]\d{2}[T ]"   # ISO-date or date-time start
    r"|\d{2}:\d{2}:\d{2}"             # HH:MM:SS
    r"|\[?\b(?:DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|TRACE|CRITICAL)\b\]?"
    r"|\w{3}\s+\d{1,2}\s+\d{2}:"      # syslog: Mon Jan  1 12:…
    r")"
)

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/\n\r]*={0,2}$")

# ---------------------------------------------------------------------------
# Budget tracker
# ---------------------------------------------------------------------------


class _Budget:
    """Mutable cumulative byte-budget tracker."""

    __slots__ = ("max_bytes", "used")

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes: int = max(0, max_bytes)
        self.used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_bytes - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_bytes

    def consume(self, n: int) -> bool:
        """Consume *n* bytes.  Returns ``True`` if budget still has room."""
        self.used += n
        return not self.exhausted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


def _is_base64(s: str) -> bool:
    """Heuristic: long, valid chars, length a multiple of 4 (after stripping)."""
    stripped = s.strip()
    if len(stripped) < _BASE64_MIN_LENGTH:
        return False
    cleaned = stripped.replace("\n", "").replace("\r", "")
    if len(cleaned) % 4 != 0:
        return False
    return bool(_BASE64_RE.match(cleaned))


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _is_log_like(s: str) -> bool:
    lines = s.splitlines()
    if len(lines) < 3:
        return False
    matching = sum(1 for ln in lines if ln and _LOG_LINE_RE.match(ln))
    return matching >= len(lines) * 0.6


# ---------------------------------------------------------------------------
# String-level transformations
# ---------------------------------------------------------------------------


def _json_summary_parts(value: Any, depth: int = 0) -> list[str]:
    if depth > 3:
        return ["…"]

    if isinstance(value, dict):
        keys = list(value.keys())
        key_list = ", ".join(repr(k) for k in keys[:8])
        if len(keys) > 8:
            key_list += f", … ({len(keys)} total)"
        parts = [f"object {{{key_list}}}"]
        for k, v in list(value.items())[:3]:
            parts.append(
                f"  {k!r}: {', '.join(_json_summary_parts(v, depth + 1))}"
            )
        if len(value) > 3:
            parts.append(f"  … ({len(value)} keys)")
        return parts

    if isinstance(value, list):
        parts = [f"array[{len(value)}]"]
        for item in value[:3]:
            parts.append(f"  {', '.join(_json_summary_parts(item, depth + 1))}")
        if len(value) > 3:
            parts.append(f"  … ({len(value)} items)")
        return parts

    if isinstance(value, str):
        preview = value[:60] + ("…" if len(value) > 60 else "")
        return [f"string({len(value)}): {preview!r}"]

    if isinstance(value, bool):
        return ["bool: " + str(value).lower()]
    if isinstance(value, int):
        return ["int: " + str(value)]
    if isinstance(value, float):
        return ["float: " + repr(value)]
    if value is None:
        return ["null"]

    return [type(value).__name__]


def _summarize_json(s: str, budget: _Budget) -> str:
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return _truncate_string(s, budget)

    parts = _json_summary_parts(parsed)
    text = _JSON_SUMMARY_PREFIX + ", ".join(parts) + _JSON_SUMMARY_SUFFIX
    byte_len = _utf8_len(text)

    if budget.remaining >= byte_len:
        budget.consume(byte_len)
        return text
    return _truncate_string(text, budget)


def _summarize_log(s: str, budget: _Budget) -> str:
    lines = s.splitlines()
    total = len(lines)

    if total <= _LOG_SUMMARY_FIRST_LINES + _LOG_SUMMARY_LAST_LINES:
        return _truncate_string(s, budget)

    kept: list[str] = lines[:_LOG_SUMMARY_FIRST_LINES]
    omitted = total - _LOG_SUMMARY_FIRST_LINES - _LOG_SUMMARY_LAST_LINES
    kept.append(f"… {omitted} line{'s' if omitted != 1 else ''} omitted …")
    kept.extend(lines[-_LOG_SUMMARY_LAST_LINES:])

    result = "\n".join(kept)
    byte_len = _utf8_len(result)

    if budget.remaining >= byte_len:
        budget.consume(byte_len)
        return result
    return _truncate_string(result, budget)


def _truncate_string(s: str, budget: _Budget) -> str:
    """Fit a plain string into the remaining budget, appending … if truncated."""
    if budget.exhausted:
        return _TRUNCATION_MARKER

    remaining = budget.remaining
    marker_bytes = _utf8_len(_TRUNCATION_MARKER)

    if remaining <= marker_bytes:
        budget.consume(remaining)
        return _TRUNCATION_MARKER

    available = remaining - marker_bytes
    encoded = s.encode("utf-8", errors="replace")

    if len(encoded) <= available:
        budget.consume(len(encoded))
        return s

    # Cut at a safe UTF-8 boundary (avoid splitting multi-byte sequences).
    cut = available
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1

    truncated = encoded[:cut].decode("utf-8", errors="replace") + _TRUNCATION_MARKER
    budget.consume(_utf8_len(truncated))
    return truncated


# ---------------------------------------------------------------------------
# Core recursive limiter
# ---------------------------------------------------------------------------


def _apply(result: Any, budget: _Budget, depth: int) -> Any:
    """Recursively limit *result* to fit within *budget*."""

    if budget.exhausted or depth > _MAX_DEPTH:
        return _TRUNCATION_MARKER

    # --- Binary data ---
    if isinstance(result, (bytes, bytearray)):
        placeholder = _BINARY_PLACEHOLDER.format(size=len(result))
        ph_bytes = _utf8_len(placeholder)
        if budget.consume(ph_bytes):
            return placeholder
        return _TRUNCATION_MARKER

    # --- Strings ---
    if isinstance(result, str):
        if _is_base64(result):
            placeholder = _BASE64_PLACEHOLDER.format(length=len(result))
            ph_bytes = _utf8_len(placeholder)
            if budget.consume(ph_bytes):
                return placeholder
            return _TRUNCATION_MARKER

        if _is_json(result):
            return _summarize_json(result, budget)

        if _is_log_like(result):
            return _summarize_log(result, budget)

        return _truncate_string(result, budget)

    # --- Lists / tuples ---
    if isinstance(result, (list, tuple)):
        return _apply_sequence(list(result), budget, depth)

    # --- Dicts ---
    if isinstance(result, dict):
        return _apply_dict(result, budget, depth)

    # --- Scalars (int, float, bool, None) pass through with tiny cost ---
    budget.consume(_SMALL_SCALAR_COST)
    return result


def _apply_sequence(items: list, budget: _Budget, depth: int) -> list:
    if budget.exhausted:
        return [_TRUNCATION_MARKER]

    if not budget.consume(_STRUCTURAL_OVERHEAD_BRACKETS):
        return [_TRUNCATION_MARKER]

    per_item_cap = max(
        _PER_ITEM_MIN_BYTES,
        int(budget.remaining * _PER_ITEM_BUDGET_FRACTION),
    )

    limited: list[Any] = []

    for item in items:
        if budget.exhausted:
            limited.append(_TRUNCATION_MARKER)
            break

        before = budget.used
        limited_item = _apply(item, budget, depth + 1)
        consumed = budget.used - before

        # If one item swallowed more than the per-item cap, revert and
        # re-process with a clamped sub-budget.
        if consumed > per_item_cap:
            budget.used = before
            sub = _Budget(per_item_cap)
            limited_item = _apply(item, sub, depth + 1)
            budget.consume(sub.used)

        budget.consume(_STRUCTURAL_OVERHEAD_PER_LIST_ITEM)
        limited.append(limited_item)

    return limited


def _apply_dict(mapping: dict, budget: _Budget, depth: int) -> dict:
    if budget.exhausted:
        return {_TRUNCATION_MARKER: _TRUNCATION_MARKER}

    if not budget.consume(_STRUCTURAL_OVERHEAD_BRACKETS):
        return {_TRUNCATION_MARKER: _TRUNCATION_MARKER}

    per_item_cap = max(
        _PER_ITEM_MIN_BYTES,
        int(budget.remaining * _PER_ITEM_BUDGET_FRACTION),
    )

    limited: dict[str, Any] = {}

    for key, value in mapping.items():
        if budget.exhausted:
            limited[_TRUNCATION_MARKER] = _TRUNCATION_MARKER
            break

        # Cost of the key in JSON: "key":
        key_cost = _utf8_len(key) + 3  # two quotes + colon
        if not budget.consume(key_cost):
            limited[_TRUNCATION_MARKER] = _TRUNCATION_MARKER
            break

        before = budget.used
        limited_value = _apply(value, budget, depth + 1)
        consumed = budget.used - before

        if consumed > per_item_cap:
            budget.used = before
            sub = _Budget(per_item_cap)
            limited_value = _apply(value, sub, depth + 1)
            budget.consume(sub.used)

        budget.consume(_STRUCTURAL_OVERHEAD_PER_DICT_ENTRY)
        limited[key] = limited_value

    return limited


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_result_limit(result: Any, max_bytes: int) -> Any:
    """Enforce a cumulative byte budget on *result*.

    Traverses lists, tuples and dicts recursively.  Strings are truncated
    at UTF-8 byte boundaries.  Large base64 blobs and raw binary data are
    replaced with lightweight placeholders.  JSON and log-formatted strings
    are summarised structurally rather than truncated naively.

    A per-item cap (fraction of remaining budget) prevents any single
    collection element from monopolising the budget; overall budget
    exhaustion is the true governor.

    Args:
        result: The MCP tool result (``dict | list | str | None`` etc.).
        max_bytes: Cumulative byte budget approximating eventual UTF-8 /
            JSON-encoded size.

    Returns:
        A structurally identical object that fits within the budget.
    """
    if max_bytes <= 0:
        return _TRUNCATION_MARKER

    budget = _Budget(max_bytes)
    return _apply(result, budget, depth=0)
