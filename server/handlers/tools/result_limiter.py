"""Clamp oversized tool results before forwarding.

Restored minimal version (2026-08-18): the storm-era PR body is gone;
behavior = hard clamp with marker. ponytail: no smart summarization —
add if oversized results actually hurt.
"""
import json
from typing import Any


def apply_result_limit(result: Any, max_bytes: int) -> Any:
    """Return result unchanged if it serializes under max_bytes, else a clamped envelope."""
    try:
        encoded = json.dumps(result)
    except (TypeError, ValueError):
        return result
    if len(encoded) <= max_bytes:
        return result
    preview = encoded[: max(0, max_bytes - 120)]
    return {
        "truncated": True,
        "max_result_size_bytes": max_bytes,
        "original_size_bytes": len(encoded),
        "preview": preview,
    }
