"""Adaptive per-tool timeouts.

Config-gated strict no-op: when ``adaptive_timeouts.enabled`` is false
(the default — and absent from stock mcproxy.json), callers get False /
the default timeout and behavior is identical to pre-feature code.

# ponytail: stub — bumble-bot referenced this module but never committed it.
# Real implementation (per-tool rolling-latency estimates) belongs here if
# adaptive timeouts are ever enabled in config.
"""

from typing import Any, Dict

_DEFAULT_TIMEOUT_SECS = 120

# Set from loaded config by main at startup; empty = feature off.
_state: Dict[str, Any] = {}


def configure(config: Dict[str, Any]) -> None:
    """Seed module state from parsed mcproxy.json (called by main)."""
    _state.clear()
    _state.update((config or {}).get("adaptive_timeouts", {}))


def is_adaptive_timeouts_enabled() -> bool:
    return bool(_state.get("enabled", False))


def get_tool_timeout(prefixed_tool_name: str) -> int:
    """Current timeout estimate for a tool (seconds)."""
    return int(_state.get("default_timeout_secs", _DEFAULT_TIMEOUT_SECS))


def record_latency(prefixed_tool_name: str, latency_ms: float) -> None:
    """Feed a observed latency into estimates. No-op in the stub."""
    return None
