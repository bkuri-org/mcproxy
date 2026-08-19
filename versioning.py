"""
Version registry with strict load-time validation, alias/default resolution,
sanitized usage tracking, deprecation warnings, and reset-on-reload support.
"""

import logging
import re
import warnings
from collections import defaultdict
from typing import Any, Callable, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Strict charsets for version keys, aliases, and tool names
_NAME_CHARSET = re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]*$')
_VERSION_CHARSET = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$')


class VersionConfigError(Exception):
    """Raised at load time when versioning configuration is invalid."""
    pass


class VersionResolutionError(Exception):
    """Raised when a version spec cannot be resolved at runtime."""
    pass


class VersionRegistry:
    """Registry for versioned tools with strict load-time validation.

    Features:
    - Charset-validated name:version parsing
    - Alias/default resolution against existing sibling entries (load-time only, no auto-derivation)
    - Sanitized usage tracking
    - Deprecation warnings
    - Reset-on-reload support
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._defaults: Dict[str, str] = {}
        self._aliases: Dict[Tuple[str, str], str] = {}
        self._deprecated: Set[Tuple[str, str]] = set()
        self._usage: Dict[Tuple[str, str], int] = defaultdict(int)
        self._resolved_names: Dict[Tuple[str, str], str] = {}

    @property
    def tools(self) -> Dict[str, Dict[str, Any]]:
        return self._tools

    @property
    def defaults(self) -> Dict[str, str]:
        return self._defaults

    def load(self, config: Dict[str, Any]) -> None:
        """Parse and validate the entire versioning configuration at load time.

        Expected structure::

            {
                "tool_name": {
                    "default_version": "v2",
                    "versions": {
                        "v1": { ... tool config ... },
                        "v2": { ... tool config ... },
                    },
                    "aliases": {"latest": "v2"},        # optional
                    "deprecated": ["v1"],               # optional
                }
            }

        Raises:
            VersionConfigError: On any validation failure with a clear, immediate message.
        """
        self.reset()

        if not config:
            return

        if not isinstance(config, dict):
            raise VersionConfigError(
                f"Versioning config must be a dict, got {type(config).__name__}"
            )

        for tool_name, tool_config in config.items():
            if not _NAME_CHARSET.match(tool_name):
                raise VersionConfigError(
                    f"Tool name '{tool_name}' contains invalid characters. "
                    f"Must match {_NAME_CHARSET.pattern}"
                )

            if not isinstance(tool_config, dict):
                raise VersionConfigError(
                    f"Config for tool '{tool_name}' must be a dict, "
                    f"got {type(tool_config).__name__}"
                )

            # --- versions map: must exist and be non-empty ---
            versions = tool_config.get("versions")
            if not versions or not isinstance(versions, dict):
                raise VersionConfigError(
                    f"Tool '{tool_name}': 'versions' must be a non-empty dict"
                )

            for version_key in versions:
                if not _VERSION_CHARSET.match(version_key):
                    raise VersionConfigError(
                        f"Tool '{tool_name}': version key '{version_key}' "
                        f"contains invalid characters. "
                        f"Must match {_VERSION_CHARSET.pattern}"
                    )

            # --- default_version: required, must be a key of versions ---
            default_version = tool_config.get("default_version")
            if default_version is None:
                raise VersionConfigError(
                    f"Tool '{tool_name}': 'default_version' is required"
                )
            if not isinstance(default_version, str):
                raise VersionConfigError(
                    f"Tool '{tool_name}': 'default_version' must be a string, "
                    f"got {type(default_version).__name__}"
                )
            if default_version not in versions:
                raise VersionConfigError(
                    f"Tool '{tool_name}': default_version '{default_version}' "
                    f"is not a key in the versions map. "
                    f"Available versions: {sorted(versions.keys())}"
                )

            # --- aliases: each must point to an existing sibling version key ---
            aliases = tool_config.get("aliases", {})
            if not isinstance(aliases, dict):
                raise VersionConfigError(
                    f"Tool '{tool_name}': 'aliases' must be a dict, "
                    f"got {type(aliases).__name__}"
                )
            for alias_name, target in aliases.items():
                if not _VERSION_CHARSET.match(alias_name):
                    raise VersionConfigError(
                        f"Tool '{tool_name}': alias '{alias_name}' "
                        f"contains invalid characters. "
                        f"Must match {_VERSION_CHARSET.pattern}"
                    )
                if not isinstance(target, str):
                    raise VersionConfigError(
                        f"Tool '{tool_name}': alias '{alias_name}' target "
                        f"must be a string, got {type(target).__name__}"
                    )
                if target not in versions:
                    raise VersionConfigError(
                        f"Tool '{tool_name}': alias '{alias_name}' points to "
                        f"'{target}' which is not a version key. "
                        f"Available versions: {sorted(versions.keys())}"
                    )
                # No auto-derivation — explicit sibling mapping only
                self._aliases[(tool_name, alias_name)] = target

            # --- deprecated: each entry must be an existing version key ---
            deprecated = tool_config.get("deprecated", [])
            if not isinstance(deprecated, list):
                raise VersionConfigError(
                    f"Tool '{tool_name}': 'deprecated' must be a list, "
                    f"got {type(deprecated).__name__}"
                )
            for dep_ver in deprecated:
                if not isinstance(dep_ver, str):
                    raise VersionConfigError(
                        f"Tool '{tool_name}': deprecated entry must be a string, "
                        f"got {type(dep_ver).__name__}"
                    )
                if dep_ver not in versions:
                    raise VersionConfigError(
                        f"Tool '{tool_name}': deprecated version '{dep_ver}' "
                        f"is not a version key. "
                        f"Available versions: {sorted(versions.keys())}"
                    )
                self._deprecated.add((tool_name, dep_ver))

            # Store validated data
            self._tools[tool_name] = versions
            self._defaults[tool_name] = default_version

            # Build resolved-name map:
            #   default version  -> original unversioned name  (e.g. "my_tool")
            #   other versions   -> "my_tool:v1"               (suffixed)
            for version_key in versions:
                if version_key == default_version:
                    self._resolved_names[(tool_name, version_key)] = tool_name
                else:
                    self._resolved_names[(tool_name, version_key)] = (
                        f"{tool_name}:{version_key}"
                    )

    def resolve(
        self, tool_name: str, version_spec: Optional[str] = None
    ) -> Tuple[str, str, Any]:
        """Resolve a tool name + optional version specifier.

        Args:
            tool_name: Base tool name (must have been loaded).
            version_spec: Version key, alias, or ``None`` to use the default.

        Returns:
            ``(resolved_tool_name, canonical_version_key, tool_config)`` where
            *resolved_tool_name* is the original name for the default version
            and ``name:version`` for every other version.

        Raises:
            VersionResolutionError: If the tool or version cannot be resolved.
        """
        if tool_name not in self._tools:
            raise VersionResolutionError(
                f"Tool '{tool_name}' is not registered in the version registry"
            )

        versions = self._tools[tool_name]

        if version_spec is None:
            canonical_key = self._defaults[tool_name]
        elif (tool_name, version_spec) in self._aliases:
            canonical_key = self._aliases[(tool_name, version_spec)]
        elif version_spec in versions:
            canonical_key = version_spec
        else:
            raise VersionResolutionError(
                f"Tool '{tool_name}': version/alias '{version_spec}' not found. "
                f"Available versions: {sorted(versions.keys())}"
            )

        # Sanitized usage tracking — keys are always validated tuples of
        # charset-checked strings established at load time.
        self._usage[(tool_name, canonical_key)] += 1

        # Deprecation warning
        if (tool_name, canonical_key) in self._deprecated:
            resolved_name = self._resolved_names[(tool_name, canonical_key)]
            warnings.warn(
                f"Tool '{resolved_name}' (version '{canonical_key}') is deprecated",
                DeprecationWarning,
                stacklevel=3,
            )

        resolved_name = self._resolved_names[(tool_name, canonical_key)]
        return resolved_name, canonical_key, versions[canonical_key]

    def get_registered_names(self) -> Dict[str, Tuple[str, str, Any]]:
        """Return the complete name -> metadata mapping for tool registration.

        The default version retains the original unversioned name; all
        non-default versions appear with a ``:version`` suffix.
        """
        result: Dict[str, Tuple[str, str, Any]] = {}
        for tool_name, versions in self._tools.items():
            for version_key, tool_config in versions.items():
                resolved_name = self._resolved_names[(tool_name, version_key)]
                result[resolved_name] = (tool_name, version_key, tool_config)
        return result

    def get_stats(self) -> Dict[str, Any]:
        """Return sanitized usage statistics (safe for admin endpoint)."""
        stats: Dict[str, Any] = {}
        for tool_name in sorted(self._tools):
            tool_stats: Dict[str, Any] = {}
            for version_key in sorted(self._tools[tool_name]):
                key = (tool_name, version_key)
                resolved_name = self._resolved_names[key]
                tool_stats[resolved_name] = {
                    "version": version_key,
                    "is_default": version_key == self._defaults[tool_name],
                    "is_deprecated": key in self._deprecated,
                    "usage_count": self._usage.get(key, 0),
                }
            stats[tool_name] = tool_stats
        return stats

    def mark_version(
        self, tool_name: str, version: str, deprecated: bool = True
    ) -> None:
        """Dynamically mark or unmark a version as deprecated after load time.

        Args:
            tool_name: Registered tool name.
            version: Version key that must already exist in the registry.
            deprecated: ``True`` to deprecate, ``False`` to un-deprecate.

        Raises:
            VersionResolutionError: If the tool or version is not registered.
        """
        if tool_name not in self._tools:
            raise VersionResolutionError(
                f"Tool '{tool_name}' is not registered in the version registry"
            )
        versions = self._tools[tool_name]
        if version not in versions:
            raise VersionResolutionError(
                f"Tool '{tool_name}': version '{version}' not found. "
                f"Available versions: {sorted(versions.keys())}"
            )
        key = (tool_name, version)
        if deprecated:
            self._deprecated.add(key)
            logger.warning(
                "Tool '%s:%s' has been marked as deprecated", tool_name, version
            )
        else:
            self._deprecated.discard(key)
            logger.info(
                "Tool '%s:%s' deprecation flag has been removed",
                tool_name,
                version,
            )

    def reset(self) -> None:
        """Clear all registry state (called automatically on reload)."""
        self._tools.clear()
        self._defaults.clear()
        self._aliases.clear()
        self._deprecated.clear()
        self._usage.clear()
        self._resolved_names.clear()


# ---------------------------------------------------------------------------
# Server integration helpers
# ---------------------------------------------------------------------------

def versioned_route(registry: VersionRegistry):
    """Factory that returns a middleware resolving versions **before** ACL.

    Wrap the existing ``api.server()`` call chain so that every incoming
    tool request passes through the registry first.  The downstream handler
    (including the ACL check) receives the *resolved* tool name.

    Usage::

        registry = VersionRegistry()
        registry.load(config)

        @versioned_route(registry)
        async def handle_tool(tool_name, context, *args, **kwargs):
            # ACL check happens here (or deeper) using context["resolved_tool_name"]
            ...
    """

    def middleware(next_handler: Callable):
        async def handler(
            tool_name: str,
            context: dict,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if tool_name in registry.tools:
                version_spec = context.pop("version_spec", None)
                try:
                    resolved_name, canonical_key, tool_config = registry.resolve(
                        tool_name, version_spec
                    )
                except VersionResolutionError as exc:
                    context["version_error"] = str(exc)
                    raise

                context["version_resolved"] = True
                context["resolved_tool_name"] = resolved_name
                context["canonical_version_key"] = canonical_key
                context["versioned_tool_config"] = tool_config

                # Logged warning when routing to a deprecated version
                if (tool_name, canonical_key) in registry._deprecated:
                    logger.warning(
                        "Routing request to deprecated tool '%s' (version '%s')",
                        resolved_name,
                        canonical_key,
                    )

                # Pass the *resolved* name onward so ACL sees the final identity
                return await next_handler(resolved_name, context, *args, **kwargs)

            # Not a versioned tool — transparent pass-through
            return await next_handler(tool_name, context, *args, **kwargs)

        return handler

    return middleware


def register_admin_stats_endpoint(
    app: Any,
    registry: VersionRegistry,
    admin_scope_check: Callable[[dict], bool],
    path: str = "/admin/version-stats",
) -> None:
    """Register an admin-only stats endpoint on *app*.

    Args:
        app: Application / router object that supports ``app.add_route(path, handler)``
             or ``app.get(path)(handler)``.  Both conventions are attempted.
        registry: The live :class:`VersionRegistry` instance.
        admin_scope_check: ``Callable(context) -> bool`` returning ``True`` when
            the caller holds the existing admin scope.
        path: URL path for the endpoint.
    """

    async def _stats_handler(context: dict) -> Any:  # type: ignore[misc]
        if not admin_scope_check(context):
            raise PermissionError("Admin scope required for version stats")
        return registry.get_stats()

    # Attempt common registration patterns; let the caller adapt if neither fits.
    if hasattr(app, "add_route"):
        app.add_route(path, _stats_handler)
    elif hasattr(app, "get"):
        app.get(path)(_stats_handler)
    else:
        raise TypeError(
            f"Cannot register stats endpoint on {type(app).__name__}: "
            "expected 'add_route' or 'get' method"
        )


def migration_hint(old: str, new: str) -> str:
    """Return a human-readable migration hint string for callers.

    Useful in deprecation warnings or error messages to guide users toward
    the replacement tool/version.

    Args:
        old: The deprecated tool name or ``name:version`` specifier.
        new: The recommended replacement tool name or ``name:version`` specifier.

    Returns:
        A short hint string, e.g. ``"Migrate from 'tool:v1' to 'tool:v2'"``.
    """
    return f"Migrate from '{old}' to '{new}'"
