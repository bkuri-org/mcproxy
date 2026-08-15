"""Sanitization utilities for error formatting and content safety.

Provides:
- Error-formatting sanitizer that scrubs enriched content before echoing
- Per-request fuzzy=True opt-in for search (execute remains exact-match)
- Schema inlining on single fuzzy match, gated by inspect scope with
  describe-pointer fallback
- Execute parameter error enrichment with schema, hints, and example
- All enriched output sanitized and returned only to the invoking caller;
  never logged verbatim.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: Sequence[re.Pattern[str]] = [
    re.compile(r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"]?[^\s'\"']+"),
    re.compile(r"(?i)(secret|token|password|passwd)\s*[=:]\s*['\"]?[^\s'\"']+"),
    re.compile(r"(?i)bearer\s+[^\s]+"),
    re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)authorization\s*:\s*[^\n]+"),
]

_MAX_SANITIZE_DEPTH = 8

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ErrorCategory(enum.Enum):
    """High-level classification for tool-parameter errors."""

    MISSING_REQUIRED = "missing_required"
    INVALID_TYPE = "invalid_type"
    OUT_OF_RANGE = "out_of_range"
    PATTERN_MISMATCH = "pattern_mismatch"
    EXTRA_FORBIDDEN = "extra_forbidden"
    ENUM_MISMATCH = "enum_mismatch"
    SCHEMA_VIOLATION = "schema_violation"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FuzzySearchOpts:
    """Per-request options governing fuzzy search behaviour.

    Attributes:
        enabled: When ``True``, tool search may fall back to fuzzy matching.
            **Search-only** — ``execute`` always requires an exact tool name.
            There is no global default; each request must explicitly opt in.
        threshold: Minimum similarity score (0.0–1.0) to consider a match.
        max_results: Upper bound on returned fuzzy candidates.
    """

    enabled: bool = False
    threshold: float = 0.7
    max_results: int = 5


@dataclass(frozen=True)
class ParamErrorDetail:
    """Enriched description of a single parameter validation failure."""

    category: ErrorCategory
    param_name: str
    message: str
    schema_snippet: Optional[str] = None
    hint: Optional[str] = None
    example: Optional[str] = None


@dataclass
class SanitizedError:
    """Container for a fully sanitized, caller-bound error payload.

    The ``_raw`` field is intentionally excluded from ``as_dict()`` and must
    never be written to logs.  Only the sanitized ``summary`` and
    ``details`` are returned to the invoking caller.
    """

    summary: str
    details: List[ParamErrorDetail] = field(default_factory=list)
    _raw: Optional[str] = field(default=None, repr=False, compare=False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "details": [
                {
                    "category": d.category.value,
                    "param_name": d.param_name,
                    "message": d.message,
                    "schema_snippet": d.schema_snippet,
                    "hint": d.hint,
                    "example": d.example,
                }
                for d in self.details
            ],
        }


# ---------------------------------------------------------------------------
# Core sanitizer
# ---------------------------------------------------------------------------


def sanitize_text(text: str, *, depth: int = 0) -> str:
    """Return *text* with sensitive patterns redacted.

    This is the single point through which **all** enriched and
    downstream-echoed content must pass before being returned to the
    invoking caller.  It is intentionally **not** used for log lines —
    callers must log only the non-enriched summary.
    """
    if depth > _MAX_SANITIZE_DEPTH:
        return "[…recursion limit…]"
    result = text
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def sanitize_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively scrub a value (dict / list / primitive) of secrets."""
    if depth > _MAX_SANITIZE_DEPTH:
        return "[…recursion limit…]"
    if isinstance(value, str):
        return sanitize_text(value, depth=depth + 1)
    if isinstance(value, dict):
        return {
            sanitize_value(k, depth=depth + 1): sanitize_value(v, depth=depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(
            sanitize_value(item, depth=depth + 1) for item in value
        )
    return value


# ---------------------------------------------------------------------------
# Fuzzy-search opt-in helpers (search-only; execute stays exact-match)
# ---------------------------------------------------------------------------


def require_fuzzy_opts(fuzzy_flag: Any) -> FuzzySearchOpts:
    """Normalise a caller-supplied *fuzzy_flag* into ``FuzzySearchOpts``.

    The flag is accepted **only** on search requests.  Passing it on an
    execute request is a logic error that callers must guard against —
    this helper simply normalises the value without enforcing scope.
    """
    if isinstance(fuzzy_flag, FuzzySearchOpts):
        return fuzzy_flag
    if isinstance(fuzzy_flag, bool):
        return FuzzySearchOpts(enabled=fuzzy_flag)
    if isinstance(fuzzy_flag, dict):
        return FuzzySearchOpts(
            enabled=bool(fuzzy_flag.get("enabled", False)),
            threshold=float(fuzzy_flag.get("threshold", 0.7)),
            max_results=int(fuzzy_flag.get("max_results", 5)),
        )
    return FuzzySearchOpts()


def is_fuzzy_allowed(scope: str, opts: FuzzySearchOpts) -> bool:
    """Return ``True`` only when *scope* is ``"search"`` **and** opts enabled.

    Execute requests always return ``False`` regardless of *opts*, ensuring
    execute stays exact-match with no global default.
    """
    return scope == "search" and opts.enabled


# ---------------------------------------------------------------------------
# Schema inlining on single fuzzy match (inspect-gated, describe-pointer
# fallback)
# ---------------------------------------------------------------------------


def inline_schema_on_fuzzy_match(
    *,
    match_name: str,
    match_count: int,
    inspect_scope_ok: bool,
    describe_pointer: Optional[Callable[[str], Optional[str]]] = None,
    schema_resolver: Optional[Callable[[str], Any]] = None,
) -> Optional[str]:
    """Conditionally inline a tool's schema when there is exactly one fuzzy match.

    The inlining is **gated** by ``inspect_scope_ok`` — the caller must have
    verified that the requesting principal holds the ``inspect`` scope.  If
    the scope check fails but a ``describe_pointer`` callable is supplied,
    it is invoked as a fallback to produce a short, non-sensitive pointer
    (e.g. a documentation URL or help-command hint) instead of the full schema.

    Returns ``None`` when:
    * ``match_count`` ≠ 1
    * Neither the inspect gate nor the describe-pointer fallback yields content
    """
    if match_count != 1:
        return None

    # Primary path: inspect scope granted → resolve full schema
    if inspect_scope_ok:
        if schema_resolver is not None:
            try:
                raw_schema = schema_resolver(match_name)
                if isinstance(raw_schema, str):
                    return sanitize_text(raw_schema)
                return sanitize_text(
                    json.dumps(raw_schema, default=str, ensure_ascii=False)
                )
            except Exception:
                pass
        return None

    # Fallback path: describe-pointer (no inspect scope)
    if describe_pointer is not None:
        try:
            pointer = describe_pointer(match_name)
            if pointer and isinstance(pointer, str):
                return sanitize_text(pointer)
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Execute parameter error enrichment
# ---------------------------------------------------------------------------


def _truncate(s: str, max_len: int = 120) -> str:
    return s if len(s) <= max_len else s[: max_len - 3] + "…"


def _resolve_param_schema(
    param_name: str,
    param_schema: Optional[Dict[str, Any]],
    full_schema: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Locate the effective schema dict for *param_name*."""
    if param_schema is not None:
        return param_schema if isinstance(param_schema, dict) else None
    if full_schema and isinstance(full_schema, dict):
        props = full_schema.get("properties", {})
        candidate = props.get(param_name)
        return candidate if isinstance(candidate, dict) else None
    return None


def _build_hint(
    category: ErrorCategory,
    schema: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Derive a short, human-friendly hint from the error category and schema."""
    if not schema:
        return None
    if category == ErrorCategory.INVALID_TYPE:
        type_name = schema.get("type", "the expected type")
        return f"Expected type: {type_name}"
    if category == ErrorCategory.ENUM_MISMATCH:
        enum_vals = schema.get("enum")
        if enum_vals:
            return f"Allowed values: {', '.join(str(v) for v in enum_vals)}"
    if category == ErrorCategory.OUT_OF_RANGE:
        lo = schema.get("minimum")
        hi = schema.get("maximum")
        parts: List[str] = []
        if lo is not None:
            parts.append(f"minimum {lo}")
        if hi is not None:
            parts.append(f"maximum {hi}")
        if parts:
            return "Must satisfy: " + ", ".join(parts)
    if category == ErrorCategory.PATTERN_MISMATCH:
        pat = schema.get("pattern")
        if pat:
            return f"Must match pattern: {pat}"
    if category == ErrorCategory.MISSING_REQUIRED:
        return "This parameter is required"
    return None


def _build_example(
    param_name: str,
    schema: Optional[Dict[str, Any]],
    example_gen: Optional[Callable[[str, Optional[Dict[str, Any]]]], Any]],
) -> Optional[str]:
    """Invoke *example_gen* and sanitize the result."""
    if example_gen is None:
        return None
    try:
        raw_example = example_gen(param_name, schema)
        if raw_example is not None:
            return sanitize_text(
                _truncate(json.dumps(raw_example, default=str, ensure_ascii=False))
            )
    except Exception:
        pass
    return None


def enrich_param_error(
    *,
    category: ErrorCategory,
    param_name: str,
    message: str,
    full_schema: Optional[Dict[str, Any]] = None,
    param_schema: Optional[Dict[str, Any]] = None,
    example_gen: Optional[Callable[[str, Optional[Dict[str, Any]]]], Any]] = None,
) -> ParamErrorDetail:
    """Build a ``ParamErrorDetail`` enriched with schema, hint, and example.

    Enrichment strategy (refactored inspect / example_gen logic):
    1. **Schema snippet** — derived from *param_schema* if available, falling
       back to the relevant sub-key inside *full_schema*.  Always truncated
       and passed through :func:`sanitize_text`.
    2. **Hint** — a short, human-friendly suggestion based on *category* and
       the available schema metadata (enum values, type name, range, pattern).
    3. **Example** — produced by calling *example_gen(param_name, schema)*
       when supplied; the result is sanitized before storage.

    The returned ``ParamErrorDetail`` is **not** to be logged verbatim —
    callers must log only a safe summary string.
    """
    schema = _resolve_param_schema(param_name, param_schema, full_schema)

    schema_snippet: Optional[str] = None
    if schema is not None:
        schema_snippet = sanitize_text(
            _truncate(json.dumps(schema, default=str, ensure_ascii=False))
        )

    hint = _build_hint(category, schema)
    example = _build_example(param_name, schema, example_gen)

    return ParamErrorDetail(
        category=category,
        param_name=param_name,
        message=sanitize_text(message),
        schema_snippet=schema_snippet,
        hint=hint,
        example=example,
    )


# ---------------------------------------------------------------------------
# Top-level: build a sanitized error envelope
# ---------------------------------------------------------------------------


def build_sanitized_error(
    summary: str,
    *,
    param_errors: Optional[List[ParamErrorDetail]] = None,
    raw_traceback: Optional[str] = None,
) -> SanitizedError:
    """Construct the final ``SanitizedError`` returned to the invoking caller.

    * *summary* is a short, safe string suitable for **both** the caller
      response and a terse log line (no secrets).
    * *param_errors* are the enriched details produced by
      :func:`enrich_param_error`.
    * *raw_traceback* is stored on ``_raw`` for internal debugging but is
      **never** included in ``as_dict()`` output or logs.
    """
    return SanitizedError(
        summary=sanitize_text(summary),
        details=param_errors or [],
        _raw=raw_traceback,
    )
