"""
context_propagation.py

Gates injection on an explicit tool allowlist plus schema check.
Is a strict no-op (verbatim args passthrough) when context_propagation.enabled
is false so disabled deployment cannot alter existing context parameters.
Strips context aliases (context/Context/ctx) on every forwarding path only
when enabled.
Enforces size limits at injection and in a call-site deep-sanitizer that
affects logs only, with the log filter kept as defense-in-depth.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONTEXT_ALIASES: Set[str] = {"context", "Context", "ctx"}

_DEFAULT_MAX_CONTEXT_BYTES: int = 64 * 1024  # 64 KiB
_DEFAULT_MAX_CONTEXT_ITEMS: int = 256
_DEFAULT_LOG_SANITIZER_MAX_DEPTH: int = 10
_DEFAULT_LOG_SANITIZER_MAX_STRING_LEN: int = 1024


@dataclass(frozen=True)
class ContextPropagationConfig:
    """Immutable configuration for context propagation."""

    enabled: bool = False
    allowed_tools: Set[str] = field(default_factory=set)
    max_context_bytes: int = _DEFAULT_MAX_CONTEXT_BYTES
    max_context_items: int = _DEFAULT_MAX_CONTEXT_ITEMS
    log_sanitizer_max_depth: int = _DEFAULT_LOG_SANITIZER_MAX_DEPTH
    log_sanitizer_max_string_len: int = _DEFAULT_LOG_SANITIZER_MAX_STRING_LEN
    context_schema: Optional[Dict[str, Any]] = None

    def is_tool_allowed(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools


# Module-level singleton – can be replaced at import time or via helper.
_config: ContextPropagationConfig = ContextPropagationConfig()


def configure(cfg: ContextPropagationConfig) -> None:
    """Replace the module-level configuration."""
    global _config
    if not isinstance(cfg, ContextPropagationConfig):
        raise TypeError(f"Expected ContextPropagationConfig, got {type(cfg)}")
    _config = cfg


def get_config() -> ContextPropagationConfig:
    """Return the current module-level configuration (read-only view)."""
    return _config


# ---------------------------------------------------------------------------
# Schema validation (lightweight – no external dependency required)
# ---------------------------------------------------------------------------

class SchemaValidationError(Exception):
    """Raised when a context payload fails the configured schema check."""


def _validate_against_schema(
    payload: Any,
    schema: Dict[str, Any],
    path: str = "",
) -> None:
    """Minimal recursive schema validator covering the subset we care about.

    Supported schema keywords:
      - type: str | int | float | bool | dict | list | null
      - required: list of keys (for objects)
      - properties: dict mapping key -> sub-schema (for objects)
      - items: sub-schema (for arrays)
      - additionalProperties: bool (for objects)
      - maxItems / minItems (for arrays)
      - maxLength / minLength (for strings)
      - maximum / minimum (for numbers)
    """
    if schema is None:
        return

    expected_type = schema.get("type")

    if expected_type is not None:
        type_map = {
            "str": str,
            "int": int,
            "float": (int, float),
            "bool": bool,
            "dict": dict,
            "list": list,
            "null": type(None),
        }
        if expected_type not in type_map:
            return  # unknown type constraint – skip
        if not isinstance(payload, type_map[expected_type]):
            if expected_type == "float" and isinstance(payload, int):
                pass
            else:
                raise SchemaValidationError(
                    f"At '{path}': expected type {expected_type!r}, "
                    f"got {type(payload).__name__}"
                )

    if isinstance(payload, str):
        if "maxLength" in schema and len(payload) > schema["maxLength"]:
            raise SchemaValidationError(
                f"At '{path}': string length {len(payload)} exceeds "
                f"maxLength {schema['maxLength']}"
            )
        if "minLength" in schema and len(payload) < schema["minLength"]:
            raise SchemaValidationError(
                f"At '{path}': string length {len(payload)} below "
                f"minLength {schema['minLength']}"
            )

    if isinstance(payload, (int, float)) and not isinstance(payload, bool):
        if "maximum" in schema and payload > schema["maximum"]:
            raise SchemaValidationError(
                f"At '{path}': value {payload} exceeds maximum {schema['maximum']}"
            )
        if "minimum" in schema and payload < schema["minimum"]:
            raise SchemaValidationError(
                f"At '{path}': value {payload} below minimum {schema['minimum']}"
            )

    if isinstance(payload, dict):
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)

        for key, sub_schema in props.items():
            if key in payload:
                _validate_against_schema(
                    payload[key], sub_schema, f"{path}.{key}"
                )

        required = schema.get("required", [])
        missing = [k for k in required if k not in payload]
        if missing:
            raise SchemaValidationError(
                f"At '{path}': missing required keys {missing}"
            )

        if not additional:
            extra = [k for k in payload if k not in props]
            if extra:
                raise SchemaValidationError(
                    f"At '{path}': additional properties not allowed: {extra}"
                )

    if isinstance(payload, list):
        if "maxItems" in schema and len(payload) > schema["maxItems"]:
            raise SchemaValidationError(
                f"At '{path}': array length {len(payload)} exceeds "
                f"maxItems {schema['maxItems']}"
            )
        if "minItems" in schema and len(payload) < schema["minItems"]:
            raise SchemaValidationError(
                f"At '{path}': array length {len(payload)} below "
                f"minItems {schema['minItems']}"
            )
        if "items" in schema:
            for i, item in enumerate(payload):
                _validate_against_schema(
                    item, schema["items"], f"{path}[{i}]"
                )


def _check_schema(payload: Any) -> None:
    """Validate *payload* against the configured schema (if any)."""
    schema = _config.context_schema
    if schema is not None:
        _validate_against_schema(payload, schema)


# ---------------------------------------------------------------------------
# Size enforcement
# ---------------------------------------------------------------------------

class ContextSizeLimitError(Exception):
    """Raised when a context payload exceeds the configured size limits."""


def _enforce_size_limits(payload: Any, label: str = "context") -> None:
    """Raise ContextSizeLimitError if *payload* is too large."""
    serialized = repr(payload)
    byte_size = len(serialized.encode("utf-8", errors="surrogatepass"))

    if byte_size > _config.max_context_bytes:
        raise ContextSizeLimitError(
            f"{label} payload is {byte_size} bytes, exceeds limit of "
            f"{_config.max_context_bytes} bytes"
        )

    if isinstance(payload, dict):
        if len(payload) > _config.max_context_items:
            raise ContextSizeLimitError(
                f"{label} has {len(payload)} top-level keys, exceeds limit of "
                f"{_config.max_context_items}"
            )
    elif isinstance(payload, list):
        if len(payload) > _config.max_context_items:
            raise ContextSizeLimitError(
                f"{label} has {len(payload)} items, exceeds limit of "
                f"{_config.max_context_items}"
            )


# ---------------------------------------------------------------------------
# Context alias stripping
# ---------------------------------------------------------------------------

def _strip_context_aliases(args: Dict[str, Any]) -> Dict[str, Any]:
    """Return a **new** dict with all context aliases removed."""
    return {k: v for k, v in args.items() if k not in _CONTEXT_ALIASES}


# ---------------------------------------------------------------------------
# Core: injection
# ---------------------------------------------------------------------------

def inject_context(
    tool_name: str,
    args: Dict[str, Any],
    context: Any,
) -> Dict[str, Any]:
    """Inject *context* into *args* for *tool_name* if allowed.

    Returns:
        A **new** dict with the context injected when all gates pass, or a
        shallow copy of *args* when the feature is disabled (strict no-op
        semantics -- the original dict is never mutated).

    Raises:
        SchemaValidationError: if *context* fails the configured schema.
        ContextSizeLimitError: if *context* exceeds size limits.
    """
    # --- STRICT NO-OP when disabled ---------------------------------------
    if not _config.enabled:
        # Shallow copy so callers cannot mutate the original via the return
        # value, but the content is byte-for-byte identical.
        return dict(args)

    # --- Gate: tool allowlist ----------------------------------------------
    if not _config.is_tool_allowed(tool_name):
        return dict(args)

    # --- Gate: schema check ------------------------------------------------
    _check_schema(context)

    # --- Gate: size limits -------------------------------------------------
    _enforce_size_limits(context, label=f"context for tool '{tool_name}'")

    # --- Strip any existing context aliases before re-injecting ------------
    cleaned = _strip_context_aliases(args)

    # --- Inject under a canonical key --------------------------------------
    cleaned["context"] = copy.deepcopy(context)
    return cleaned


# ---------------------------------------------------------------------------
# Core: forwarding passthrough (strip aliases when enabled)
# ---------------------------------------------------------------------------

def forward_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare *args* for forwarding to the next hop.

    * When **enabled**: strips all context aliases so context never leaks
      into unintended downstream consumers.
    * When **disabled**: strict no-op -- returns a shallow copy with the
      original keys untouched.
    """
    if not _config.enabled:
        return dict(args)
    return _strip_context_aliases(args)


# ---------------------------------------------------------------------------
# Deep sanitizer for log output only
# ---------------------------------------------------------------------------

def _deep_sanitize_for_log(
    obj: Any,
    _depth: int = 0,
    _seen: Optional[Set[int]] = None,
) -> Any:
    """Return a truncated copy of *obj* suitable for safe logging.

    This NEVER affects the actual arguments passed to tools -- it is only
    consumed by ``sanitize_for_log()`` and the log filter below.
    """
    cfg = _config
    max_depth = cfg.log_sanitizer_max_depth
    max_str = cfg.log_sanitizer_max_string_len

    if _seen is None:
        _seen = set()

    obj_id = id(obj)
    if obj_id in _seen:
        return "<cyclic reference>"
    _seen = _seen | {obj_id}

    if _depth > max_depth:
        return "<truncated: max depth exceeded>"

    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            out[k] = _deep_sanitize_for_log(v, _depth + 1, _seen)
        return out
    if isinstance(obj, list):
        return [_deep_sanitize_for_log(v, _depth + 1, _seen) for v in obj]
    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[:max_str] + f"<...truncated, was {len(obj)} chars>"
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes: {len(obj)}B>"
    return obj


# ---------------------------------------------------------------------------
# Log filter (defense-in-depth)
# ---------------------------------------------------------------------------

class _ContextPropagationLogFilter(logging.Filter):
    """Logging filter that sanitizes context payloads in log records.

    This is **defense-in-depth**: even if a caller accidentally passes the
    raw (un-sanitized) context to ``logging.info(...)`` or similar, the
    filter will truncate it before it reaches any handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not _config.enabled:
            return True
        record.msg = _deep_sanitize_for_log(record.msg)
        if record.args:
            record.args = tuple(
                _deep_sanitize_for_log(a) for a in record.args
            )
        return True


# Install the filter on the root logger so it applies globally.
_root_logger = logging.getLogger()
_log_filter = _ContextPropagationLogFilter()
_root_logger.addFilter(_log_filter)


# ---------------------------------------------------------------------------
# Public convenience: sanitize for explicit log calls
# ---------------------------------------------------------------------------

def sanitize_for_log(obj: Any) -> Any:
    """Return a log-safe copy of *obj*.

    Intended for call-sites that want to explicitly sanitize before logging,
    independent of the automatic filter.  The automatic filter remains as
    defense-in-depth.
    """
    if not _config.enabled:
        return obj
    return _deep_sanitize_for_log(obj)


# ---------------------------------------------------------------------------
# Introspection helpers (useful for tests / debugging)
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return _config.enabled


def get_allowed_tools() -> Set[str]:
    return set(_config.allowed_tools)


def get_context_aliases() -> Set[str]:
    return set(_CONTEXT_ALIASES)
