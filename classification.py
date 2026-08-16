"""Server classification enforcement.

Provides a single shared ``enforce_server_classifications()`` called by both
server_manager.py startup and the config_reloader.py / config_watcher.py reload
hot-path.  Both call sites use this function identically — there is no reload
bypass.

Tiers
-----
blocked      Fail-closed via blocklist adapter.  Adapter error or unreadable
             blocklist means block — never allow.
risky        Requires ``acknowledged: true`` in the classification dict.
unclassified Warns once per server per process lifetime.
secret       Classification-only label; no enforcement is applied.

Any tier string outside the set above raises ``ConfigError`` and is never
silently degraded to unclassified.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

VALID_TIERS = frozenset({"blocked", "risky", "unclassified", "secret"})

_warned_unclassified: Set[str] = set()
_warn_lock = threading.Lock()


class ConfigError(Exception):
    """Raised when a server configuration violates classification rules."""


def enforce_server_classifications(
    server_name: str,
    server_config: Dict[str, Any],
    blocklist_adapter: Optional[Callable[[str], bool]] = None,
) -> None:
    """Validate and enforce the classification tier for *server_name*.

    Parameters
    ----------
    server_name:
        Logical server identifier used for logging and warn-once tracking.
    server_config:
        Full server configuration dict.  May contain a ``"classification"``
        key whose value is either a plain tier string or a dict with at
        least a ``"tier"`` sub-key (and, for risky, ``"acknowledged"``).
        Absence of the key is treated as unclassified.
    blocklist_adapter:
        Callable returning ``True`` when the server is on the blocklist.
        If it raises **any** exception the server is treated as blocked
        (fail-closed).  ``None`` means no adapter; a "blocked" tier still
        results in a block.

    Raises
    ------
    ConfigError
        * Invalid tier string.
        * Risky server without ``acknowledged: true``.
        * Blocked server (on blocklist, adapter error, or no adapter).
    """
    classification = server_config.get("classification")

    if classification is None:
        tier = "unclassified"
        acknowledged = False
    elif isinstance(classification, str):
        tier = classification
        acknowledged = False
    elif isinstance(classification, dict):
        tier = classification.get("tier", "unclassified")
        acknowledged = bool(classification.get("acknowledged", False))
    else:
        raise ConfigError(
            f"Server '{server_name}': 'classification' must be a string or "
            f"dict, got {type(classification).__name__}"
        )

    if tier not in VALID_TIERS:
        raise ConfigError(
            f"Server '{server_name}': invalid classification tier '{tier}'. "
            f"Valid tiers: {', '.join(sorted(VALID_TIERS))}"
        )

    # --- blocked: fail-closed ---
    if tier == "blocked":
        if blocklist_adapter is not None:
            try:
                is_blocked = blocklist_adapter(server_name)
            except Exception:
                logger.error(
                    "Server '%s': blocklist adapter raised; "
                    "fail-closed -> blocking",
                    server_name,
                    exc_info=True,
                )
                is_blocked = True
        else:
            is_blocked = True

        if is_blocked:
            reason = (
                "present on blocklist"
                if blocklist_adapter is not None
                else "no blocklist adapter configured"
            )
            raise ConfigError(
                f"Server '{server_name}': classified as 'blocked' ({reason})"
            )
        return

    # --- risky: requires acknowledged: true ---
    if tier == "risky":
        if not acknowledged:
            raise ConfigError(
                f"Server '{server_name}': classified as 'risky' but "
                f"'acknowledged' is not true in classification config"
            )
        return

    # --- secret: label only, no enforcement ---
    if tier == "secret":
        return

    # --- unclassified: warn once per server per process ---
    if tier == "unclassified":
        with _warn_lock:
            if server_name not in _warned_unclassified:
                _warned_unclassified.add(server_name)
                logger.warning(
                    "Server '%s' has no classification assigned; "
                    "treating as unclassified",
                    server_name,
                )
        return
