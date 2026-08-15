import logging
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


class DeprecationRecord:
    """Tracks the lifecycle and grace period of a deprecated parameter."""
    def __init__(self, since_version: str, removal_version: str, grace_period_days: int):
        self.since_version = since_version
        self.removal_version = removal_version
        self.grace_period_days = grace_period_days
        self.since_timestamp: float = time.time()

    def is_grace_period_expired(self) -> bool:
        """Returns True if the current time exceeds the allowed grace period."""
        return (time.time() - self.since_timestamp) > (self.grace_period_days * 86400)


class SchemaMigrationEngine:
    """
    Core engine for applying schema migrations.
    Handles chain resolution, collision precedence, and deprecation blocking.
    """
    def __init__(self, config: Dict[str, Any]):
        self._mappings: Dict[str, str] = config.get("mappings", {})
        self._deprecations: Dict[str, DeprecationRecord] = {}
        
        for key, dep_config in config.get("deprecations", {}).items():
            record = DeprecationRecord(
                since_version=dep_config.get("since", "0.0.0"),
                removal_version=dep_config.get("remove_at", "999.0.0"),
                grace_period_days=dep_config.get("grace_period_days", 30)
            )
            # Allow main.py to inject the exact activation timestamp if needed
            if "since_timestamp" in dep_config:
                record.since_timestamp = dep_config["since_timestamp"]
            self._deprecations[key] = record

    def _resolve_chain_fixpoint(self, param_name: str) -> str:
        """
        Resolves transitive mappings (e.g., A -> B -> C becomes A -> C).
        Includes a cycle guard to prevent infinite loops on malformed configs.
        """
        visited: Set[str] = set()
        current = param_name
        
        while current in self._mappings:
            if current in visited:
                logger.error(
                    "Cycle detected in schema migration mapping for '%s'. "
                    "Chain resolved up to cycle: %s", 
                    param_name, " -> ".join(visited)
                )
                break
            visited.add(current)
            current = self._mappings[current]
            
        return current

    def _enforce_deprecation_policy(self, original_params: Dict[str, Any]) -> None:
        """
        Audits parameters against the deprecation registry.
        Blocks execution if a deprecated parameter is used outside its grace period.
        """
        for key in original_params:
            if key in self._deprecations:
                record = self._deprecations[key]
                target = self._resolve_chain_fixpoint(key)
                
                if record.is_grace_period_expired():
                    raise RuntimeError(
                        f"Blocked request: Parameter '{key}' (migrated to '{target}') "
                        f"has exceeded its {record.grace_period_days}-day grace period "
                        f"(since {record.since_version}, removal at {record.removal_version})."
                    )
                else:
                    logger.warning(
                        "DeprecationWarning: Parameter '%s' is deprecated and will be "
                        "removed in version %s. Please migrate to '%s'.",
                        key, record.removal_version, target
                    )

    def migrate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Applies parameter mapping with 'mapped-wins' collision precedence.
        
        If a collision occurs (both old_key and new_key are present in params),
        the value of the mapped key (new_key) wins and the old key is discarded.
        """
        if not self._mappings and not self._deprecations:
            return params

        # 1. Enforce deprecation policy strictly before modifying the payload
        self._enforce_deprecation_policy(params)

        migrated_params = params.copy()
        keys_to_remove = []

        # 2. Determine final targets for all present keys
        remapping_plan: Dict[str, str] = {}
        for key in list(migrated_params.keys()):
            if key in self._mappings:
                final_target = self._resolve_chain_fixpoint(key)
                remapping_plan[key] = final_target

        # 3. Apply "mapped-wins" collision precedence
        for old_key, new_key in remapping_plan.items():
            if new_key not in migrated_params:
                # No collision: safely transfer the value to the new key
                migrated_params[new_key] = migrated_params[old_key]
            # If new_key IS in migrated_params, we do nothing (mapped-wins: 
            # the explicitly provided new_key value takes precedence).
            keys_to_remove.append(old_key)

        # 4. Cleanup old keys
        for key in keys_to_remove:
            migrated_params.pop(key, None)

        return migrated_params


_global_engine: Optional[SchemaMigrationEngine] = None


def initialize_engine(config: Dict[str, Any]) -> None:
    """
    Initializes the global SchemaMigrationEngine instance.
    Must be called once by main.py with the loaded 'schema_migrations' config.
    """
    global _global_engine
    _global_engine = SchemaMigrationEngine(config)


def apply_migration(
    params: Dict[str, Any], 
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Shared helper to apply schema migrations to a parameter dictionary.
    
    This acts as the single choke point for schema rewrites before lookups.
    While router.py is the primary choke point, http_backend.py and api_parallel.py
    can call this helper directly if they must bypass the router, ensuring 
    identical migration logic and blocking audits across the codebase.
    
    Args:
        params: The original parameters payload.
        config: Optional specific config for a transient engine. If omitted, 
                uses the globally initialized engine fed by main.py.
                
    Returns:
        The migrated parameters dictionary.
        
    Raises:
        RuntimeError: If the global engine is uninitialized and no config is provided,
                      or if a deprecated parameter is past its grace period.
    """
    if config is not None:
        transient_engine = SchemaMigrationEngine(config)
        return transient_engine.migrate(params)
        
    if _global_engine is None:
        raise RuntimeError(
            "SchemaMigrationEngine not initialized. Ensure main.py loads and passes "
            "the 'schema_migrations' config via schema_migration.initialize_engine()."
        )
        
    return _global_engine.migrate(params)
