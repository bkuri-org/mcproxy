"""Online blocklist system for MCProxy v4.2.

Features:
- GitHub-hosted blocklist.json with periodic sync
- Local cache with offline fallback to embedded blocklist
- Server capability classification (safe/network/secret/risky)
- Startup validation against blocked servers
- Hot reload: Re-check on config changes
- Manual refresh: POST /admin/blocklist/refresh
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

import aiohttp

from logging_config import get_logger

logger = get_logger(__name__)


class ServerTier(Enum):
    """Server capability tiers."""

    SAFE = "safe"
    NETWORK = "network"
    SECRET = "secret"
    RISKY = "risky"


@dataclass
class ServerClassification:
    """Server classification entry."""

    tier: ServerTier
    reasons: List[str]
    severity: Optional[str] = None
    requires_ack: bool = False
    cve: Optional[str] = None
    migrated_to: Optional[str] = None


EMBEDDED_BLOCKLIST = {
    "version": "1.0.0",
    "updated": "2024-01-01T00:00:00Z",
    "blocked": {
        "@executeautomation/tmux-mcp-server": {
            "reasons": [
                "arbitrary_shell_execution",
                "host_filesystem_access",
                "credential_exposure",
            ],
            "severity": "critical",
            "cve": None,
        }
    },
    "risky": {
        "@executeautomation/playwright-mcp-server": {
            "reasons": [
                "browser_automation",
                "host_filesystem_access",
                "network_access",
            ],
            "severity": "high",
            "requires_ack": True,
        },
        "jesse_mcp": {
            "reasons": ["full_python_process", "has_credentials"],
            "severity": "high",
            "requires_ack": True,
        },
    },
    "deprecated": {},
}


class Blocklist:
    """Online blocklist with sync and classification."""

    def __init__(
        self,
        config: Dict[str, Any],
        cache_dir: str = "cache",
    ):
        self._config = config
        self._cache_dir = Path(cache_dir)
        self._cache_file = self._cache_dir / "blocklist.json"
        self._blocklist: Dict[str, Any] = {}
        self._last_sync: Optional[datetime] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._initialized: bool = False

        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._config.get("blocklist_enabled", True)

    @property
    def blocklist_url(self) -> str:
        return self._config.get(
            "blocklist_url",
            "https://raw.githubusercontent.com/mcproxy/blocklist/main/blocklist.json",
        )

    @property
    def sync_interval(self) -> int:
        return self._config.get("blocklist_sync_interval", 3600)

    @property
    def allow_risky(self) -> bool:
        return self._config.get("allow_risky_servers", False)

    @property
    def risky_acknowledgments(self) -> Dict[str, str]:
        return self._config.get("risky_server_acknowledgments", {})

    async def initialize(self) -> None:
        """Initialize blocklist - load cache and start sync."""
        cached = self._load_cache()

        if cached is not None:
            self._blocklist = cached
        else:
            self._blocklist = EMBEDDED_BLOCKLIST.copy()
            self._save_cache()

        logger.info(
            f"Blocklist initialized: version={self._blocklist.get('version')}, "
            f"enabled={self.enabled}"
        )

        self._initialized = True

        if self.enabled:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def _sync_loop(self) -> None:
        """Background sync loop."""
        while True:
            try:
                await asyncio.sleep(self.sync_interval)
                await self.sync()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Blocklist sync failed: {e}")

    async def sync(self) -> Tuple[bool, str]:
        """Sync blocklist from remote URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.blocklist_url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        remote = await resp.json()

                        if remote.get("version") != self._blocklist.get("version"):
                            old_version = self._blocklist.get("version")
                            self._blocklist = remote
                            self._save_cache()
                            logger.info(
                                f"Blocklist updated: {old_version} -> {remote.get('version')}"
                            )
                            return (
                                True,
                                f"Updated from {old_version} to {remote.get('version')}",
                            )
                        else:
                            return True, "Blocklist already up to date"
                    else:
                        return False, f"HTTP {resp.status}"
        except asyncio.TimeoutError:
            return False, "Timeout fetching blocklist"
        except Exception as e:
            return False, str(e)

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        """Load blocklist from cache file."""
        if not self._cache_file.exists():
            return None
        try:
            with open(self._cache_file, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cache(self) -> None:
        """Save blocklist to cache file."""
        try:
            with open(self._cache_file, "w") as f:
                json.dump(self._blocklist, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save blocklist cache: {e}")

    def get_classification(self, package_name: str) -> Optional[ServerClassification]:
        """Get classification for a server package."""
        blocked = self._blocklist.get("blocked", {})
        if package_name in blocked:
            entry = blocked[package_name]
            return ServerClassification(
                tier=ServerTier.RISKY,
                reasons=entry.get("reasons", []),
                severity=entry.get("severity"),
                cve=entry.get("cve"),
            )

        risky = self._blocklist.get("risky", {})
        if package_name in risky:
            entry = risky[package_name]
            return ServerClassification(
                tier=ServerTier.RISKY,
                reasons=entry.get("reasons", []),
                severity=entry.get("severity"),
                requires_ack=entry.get("requires_ack", False),
            )

        deprecated = self._blocklist.get("deprecated", {})
        if package_name in deprecated:
            entry = deprecated[package_name]
            return ServerClassification(
                tier=ServerTier.RISKY,
                reasons=["deprecated"] + entry.get("reasons", []),
                severity="medium",
                migrated_to=entry.get("migrated_to"),
            )

        return None

    def validate_servers(
        self, servers: List[Dict[str, Any]]
    ) -> Tuple[List[str], List[str]]:
        """Validate servers against blocklist."""
        errors: List[str] = []
        warnings: List[str] = []

        if not self.enabled:
            return errors, warnings

        for server in servers:
            name = server.get("name", "unknown")

            # Combine command and args - mcproxy.json has them separate
            command = server.get("command", [])
            args = server.get("args", [])

            # Handle both cases: command could be a string or list
            if isinstance(command, str):
                command = [command]

            # Combine command + args into single list
            full_command = list(command) + list(args)

            package_name = self._extract_package_name(full_command)
            if not package_name:
                continue

            classification = self.get_classification(package_name)

            if classification is None:
                warnings.append(
                    f"Server '{name}' ({package_name}) is not classified. "
                    f"Consider adding to blocklist.json for safety."
                )
                continue

            if classification.cve:
                errors.append(
                    f"SECURITY BLOCK: Server '{name}' ({package_name}) has known vulnerability. "
                    f"CVE: {classification.cve}. Reasons: {', '.join(classification.reasons)}"
                )
                continue

            if classification.severity == "critical":
                errors.append(
                    f"SECURITY BLOCK: Server '{name}' ({package_name}) is blocked as critical risk. "
                    f"Reasons: {', '.join(classification.reasons)}"
                )
                continue

            if classification.requires_ack:
                if not self.allow_risky:
                    errors.append(
                        f"SECURITY BLOCK: Server '{name}' ({package_name}) is risky and not allowed. "
                        f"Set security.allow_risky_servers=true to enable. "
                        f"Reasons: {', '.join(classification.reasons)}"
                    )
                elif name not in self.risky_acknowledgments:
                    errors.append(
                        f"SECURITY BLOCK: Server '{name}' ({package_name}) requires acknowledgment. "
                        f"Add to security.risky_server_acknowledgments: {{{name}: '<reason>'}}"
                    )
                else:
                    warnings.append(
                        f"[WARNING] RISKY SERVER: '{name}' ({package_name}) is allowed with acknowledgment. "
                        f"This server has elevated privileges: {', '.join(classification.reasons)}. "
                        f"Use with caution and ensure appropriate access controls."
                    )

        return errors, warnings

    def _extract_package_name(self, command: List[str]) -> Optional[str]:
        """Extract package name from server command."""
        if not command:
            return None

        # Handle npx - could be "npx" or "/usr/bin/npx"
        for i, arg in enumerate(command):
            if arg.endswith("npx") or arg == "npx":
                if i + 1 < len(command):
                    next_arg = command[i + 1]
                    if next_arg == "-y" and i + 2 < len(command):
                        return command[i + 2]
                    return next_arg

        # Handle uvx - could be "uvx" or "/usr/bin/uvx"
        for i, arg in enumerate(command):
            if arg.endswith("uvx") or arg == "uvx":
                if i + 1 < len(command):
                    return command[i + 1]

        return None

    async def shutdown(self) -> None:
        """Shutdown blocklist sync."""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
