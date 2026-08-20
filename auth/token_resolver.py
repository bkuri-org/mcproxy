"""Token resolution: SHA-256 hashed tokens at rest, single atomic map, stateless scope resolution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TokenMap = Dict[
    str,  # sha256 hex digest
    Tuple[str, str, FrozenSet[str]],  # (type, name, frozenset(members))
]

TokenEntry = Tuple[str, str, FrozenSet[str]]  # (type, name, members)

# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def _sha256(plaintext: str) -> str:
    """Return the lowercase hex SHA-256 digest of *plaintext*."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Map builder — pure function, no side-effects
# ---------------------------------------------------------------------------

def build_token_map(
    namespaces: Dict[str, Dict],
    groups: Dict[str, Dict],
) -> TokenMap:
    """
    From a *single* config snapshot, produce a fully-expanded token map.

    *namespaces*:  {name: {"tokens": [plaintext, ...], "scope": [member, ...]}}
    *groups*:      {name: {"tokens": [plaintext, ...], "members": [member, ...]}}

    Group expansion is baked in: each group's effective members are resolved
    transitively within the *same* snapshot so there is no cross-module state
    window at reload.
    """
    expanded_groups: Dict[str, FrozenSet[str]] = {}

    def _resolve_group(name: str, visited: Set[str] | None = None) -> FrozenSet[str]:
        if name in expanded_groups:
            return expanded_groups[name]
        if visited is None:
            visited = set()
        if name in visited:
            return frozenset()  # cycle guard
        visited.add(name)
        if name not in groups:
            return frozenset()
        raw_members: list[str] = groups[name].get("members", [])
        members: Set[str] = set()
        for m in raw_members:
            if m.startswith("group:"):
                members.update(_resolve_group(m[len("group:"):], visited))
            else:
                members.add(m)
        result = frozenset(members)
        expanded_groups[name] = result
        return result

    # Pre-resolve all groups
    for gname in groups:
        _resolve_group(gname)

    token_map: TokenMap = {}

    # Namespace tokens
    for ns_name, ns_cfg in namespaces.items():
        for plaintext in ns_cfg.get("tokens", []):
            h = _sha256(plaintext)
            scope = frozenset(ns_cfg.get("scope", []))
            token_map[h] = ("namespace", ns_name, scope)

    # Group tokens — members are the *expanded* set
    for g_name, g_cfg in groups.items():
        for plaintext in g_cfg.get("tokens", []):
            h = _sha256(plaintext)
            members = expanded_groups.get(g_name, frozenset())
            token_map[h] = ("group", g_name, members)

    return token_map

# ---------------------------------------------------------------------------
# Atomic holder — single mutable slot, swapped atomically
# ---------------------------------------------------------------------------

class TokenMapHolder:
    """Single mutable slot, swapped atomically via the GIL + single reference."""

    __slots__ = ("_map",)

    def __init__(self, initial: TokenMap | None = None) -> None:
        self._map: TokenMap = initial if initial is not None else {}

    @property
    def current(self) -> TokenMap:
        return self._map

    def try_swap(self, candidate: TokenMap) -> bool:
        """
        Atomically replace the current map with *candidate*.

        Validation is the caller's responsibility — this method always
        succeeds (the GIL guarantees atomic assignment of a single reference
        in CPython).  Returns ``True`` so callers can chain.

        The *or keep the old one* semantics live outside: the caller builds
        a candidate, validates it, and only calls ``try_swap`` on success.
        """
        self._map = candidate
        return True


# Global singleton — the ONLY mutable module-level state
_holder = TokenMapHolder()


def get_token_map() -> TokenMap:
    """Return the current (atomically readable) token map."""
    return _holder.current


def swap_token_map(candidate: TokenMap) -> bool:
    """Atomically install *candidate* or keep the old map (on False)."""
    return _holder.try_swap(candidate)

# ---------------------------------------------------------------------------
# Stateless scope resolver — pure functions, take the map explicitly
# ---------------------------------------------------------------------------

def resolve_token(
    token_map: TokenMap,
    plaintext_token: str,
) -> Optional[TokenEntry]:
    """Look up a plaintext token in *token_map*; returns the entry or ``None``."""
    return token_map.get(_sha256(plaintext_token))


def authorize_scope(
    token_scope: FrozenSet[str],
    request_scope: FrozenSet[str],
) -> bool:
    """
    Return ``True`` iff *token_scope* ⊆ *request_scope*.

    Every member the token grants must also be present in the request scope.
    A token whose scope is a *proper superset* of the request scope (widening)
    fails → the caller returns 403.  Equality is allowed.

    This is intentionally NOT a union — the token cannot grant more than what
    the request context allows.
    """
    return token_scope.issubset(request_scope)


def is_namespace_protected(
    namespace_name: str,
    namespace_configs: Dict[str, Dict],
) -> bool:
    """A namespace is protected if it has any tokens configured."""
    cfg = namespace_configs.get(namespace_name)
    if cfg is None:
        return False
    return bool(cfg.get("tokens"))

# ---------------------------------------------------------------------------
# Middleware helpers — return (status_code | None, token_entry | None)
# ---------------------------------------------------------------------------

def authenticate_request(
    token_map: TokenMap,
    raw_token: Optional[str],
    namespace_name: str,
    namespace_configs: Dict[str, Dict],
) -> Tuple[Optional[int], Optional[TokenEntry]]:
    """
    Step 1 of the middleware pipeline — authentication.

    Returns
    -------
    (status, entry)
        * (None, entry)  — success, *entry* is the resolved token entry
        * (None, None)   — anonymous access on an unprotected namespace
        * (401, None)    — missing token on a protected namespace, or unknown token
    """
    protected = is_namespace_protected(namespace_name, namespace_configs)

    if protected and not raw_token:
        return (401, None)

    if not raw_token:
        # Unprotected namespace, no token provided → anonymous
        return (None, None)

    entry = resolve_token(token_map, raw_token)
    if entry is None:
        return (401, None)

    return (None, entry)


def authorize_request(
    token_entry: TokenEntry,
    request_scope: FrozenSet[str],
) -> Optional[int]:
    """
    Step 2 — authorization.

    Returns
    -------
    * ``None``   — authorized (token_scope ⊆ request_scope)
    * ``403``    — token scope widens beyond request scope
    """
    _type, _name, token_scope = token_entry
    if not authorize_scope(token_scope, request_scope):
        return 403
    return None

# ---------------------------------------------------------------------------
# SSE scope binding
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SSEConnection:
    """
    Immutable scope binding for an SSE connection.

    The scope is frozen at connection time.  If the same session-id is
    replayed (e.g. after a reconnect or from a different client), the
    middleware re-authenticates and produces a *new* ``SSEConnection`` —
    the old one's scope is irrelevant and cannot be reused.
    """
    session_id: str
    scope: FrozenSet[str]
    token_type: str
    token_name: str

# ---------------------------------------------------------------------------
# Config reload orchestrator
# ---------------------------------------------------------------------------

def reload_token_map(
    namespaces: Dict[str, Dict],
    groups: Dict[str, Dict],
) -> bool:
    """
    Build a fully-expanded candidate map from the config snapshot,
    validate it, and atomically swap it in — or keep the old map on
    any failure.

    Returns ``True`` if the swap happened.
    """
    try:
        candidate = build_token_map(namespaces, groups)
        # Structural validation — every entry must be well-formed
        for h, entry in candidate.items():
            ttype, tname, members = entry
            if ttype not in ("namespace", "group"):
                return False  # keep old map
            if not isinstance(tname, str) or not tname:
                return False
            if not isinstance(members, frozenset):
                return False
        swap_token_map(candidate)
        return True
    except Exception:
        return False  # keep old map
