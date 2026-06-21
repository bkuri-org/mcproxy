"""Sandbox execution system for MCProxy v2.0.

Provides secure code execution via uv subprocess with namespace-based access control.
"""

from sandbox.access_control import NamespaceAccessControl, AccessControlConfig
from sandbox.constants import (
    get_blocked_attributes,
    get_blocked_functions,
    get_blocked_imports,
)
from sandbox.executor import SandboxExecutor
# ponytail: proxy.py deleted — executor uses runtime.py string injection
from sandbox.security import (
    BLOCKED_BUILTINS,
    BLOCKED_IMPORTS,
    FUZZY_MATCH_THRESHOLD,
    MAX_CODE_SIZE_BYTES,
    MAX_SUGGESTIONS,
    suggest_tool_fix,
)

__all__ = [
    "SandboxExecutor",
    "AccessControlConfig",
    "NamespaceAccessControl",
    "suggest_tool_fix",
    "BLOCKED_IMPORTS",
    "BLOCKED_BUILTINS",
    "MAX_CODE_SIZE_BYTES",
    "FUZZY_MATCH_THRESHOLD",
    "MAX_SUGGESTIONS",
    "get_blocked_functions",
    "get_blocked_imports",
    "get_blocked_attributes",
]
