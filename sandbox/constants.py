"""Security constants and blocklist definitions for sandbox execution.

Contains blocklists for imports, builtins, and attributes that are restricted
for security purposes.
"""

from typing import List

# Constants from security.py (re-exported for backward compatibility)
from sandbox.security import BLOCKED_BUILTINS, BLOCKED_IMPORTS, MAX_CODE_SIZE_BYTES

# --- Adaptive timeout constants (used by sandbox.utils.adaptive_timeout) ---

ADAPTIVE_TIMEOUT_MIN: float = 1.0
"""Hard floor – no computed timeout may fall below this value."""

ADAPTIVE_TIMEOUT_MAX: float = 120.0
"""Hard ceiling – no computed timeout may exceed this value."""

ADAPTIVE_TIMEOUT_DEFAULT: float = 30.0
"""Cold-start fallback returned when the rolling window is empty after trim."""

ADAPTIVE_TIMEOUT_WINDOW_SIZE: int = 50
"""Number of recent non-timeout durations kept per tool."""

ADAPTIVE_TIMEOUT_TRIM_PCT: float = 0.1
"""Fraction to trim from each tail (0.1 → p10–p90, i.e. p90 trim)."""

ADAPTIVE_TIMEOUT_SIGMA_MULT: float = 2.0
"""Multiplier applied to the standard deviation of the trimmed window."""

__all__ = [
    "BLOCKED_BUILTINS",
    "BLOCKED_IMPORTS",
    "MAX_CODE_SIZE_BYTES",
    "ADAPTIVE_TIMEOUT_MIN",
    "ADAPTIVE_TIMEOUT_MAX",
    "ADAPTIVE_TIMEOUT_DEFAULT",
    "ADAPTIVE_TIMEOUT_WINDOW_SIZE",
    "ADAPTIVE_TIMEOUT_TRIM_PCT",
    "ADAPTIVE_TIMEOUT_SIGMA_MULT",
    "get_blocked_functions",
    "get_blocked_imports",
    "get_blocked_attributes",
]


def get_blocked_functions() -> list[str]:
    """Return list of functions blocked in sandbox for security.

    Returns:
        List of blocked function names with descriptions
    """
    return [
        "eval()",
        "exec()",
        "compile()",
        "open() (file operations)",
        "input()",
        "__import__()",
        "breakpoint()",
        "hasattr()",
        "getattr()",
        "setattr()",
        "delattr()",
        "os.system()",
        "os.popen()",
        "subprocess.* (all subprocess calls)",
        "pickle.loads() / pickle.load()",
        "marshal.loads() / marshal.load()",
        "importlib.import_module()",
    ]


def get_blocked_imports() -> list[str]:
    """Return list of modules blocked from import.

    Returns:
        List of blocked module names
    """
    return [
        "os",
        "sys",
        "subprocess",
        "socket",
        "http",
        "urllib",
        "requests",
        "shutil",
        "tempfile",
        "multiprocessing",
        "pickle",
        "marshal",
        "importlib",
        "builtins",
    ]


def get_blocked_attributes() -> list[str]:
    """Return list of blocked dunder attributes.

    Returns:
        List of blocked attribute names
    """
    return [
        "__class__",
        "__bases__",
        "__subclasses__",
        "__globals__",
        "__locals__",
        "__code__",
        "__builtins__",
        "__dict__",
        "__mro__",
        "__init__",
        "__new__",
        "__reduce__",
        "__getstate__",
        "__setstate__",
    ]
