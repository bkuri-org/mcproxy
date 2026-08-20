"""Configuration loading and validation for MCProxy v2.0.

Handles loading mcproxy.json, validating schema, and interpolating
environment variables. Supports v2.0 features: namespaces, groups, manifests, sandbox.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from logging_config import get_logger

from utils.namespace import normalize_namespace_config

logger = get_logger(__name__)


class ConfigError(Exception):
    """Error loading or validating configuration."""

    pass


def load_config(path: str) -> Dict[str, Any]:
    """Load and validate configuration from JSON file.

    Args:
        path: Path to mcproxy.json file

    Returns:
        Validated configuration dictionary

    Raises:
        ConfigError: If config is invalid or file cannot be read
    """
    config_path = Path(path)

    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {path}: {e}")
    except Exception as e:
        raise ConfigError(f"Failed to read {path}: {e}")

    validate_schema(config)
    config = interpolate_env_vars(config)

    server_count = len(config.get("servers", []))
    namespace_count = len(config.get("namespaces", {}))
    logger.info(
        f"Loaded config from {path} with {server_count} servers, "
        f"{namespace_count} namespaces"
    )
    return config


def validate_schema(config: Dict[str, Any]) -> None:
    """Validate configuration schema (v2.0).

    Args:
        config: Configuration dictionary to validate

    Raises:
        ConfigError: If schema validation fails
    """
    if not isinstance(config, dict):
        raise ConfigError("Config must be a JSON object")

    if "servers" not in config:
        raise ConfigError("Config missing required 'servers' field")

    if not isinstance(config["servers"], list):
        raise ConfigError("'servers' must be an array")

    for i, server in enumerate(config["servers"]):
        validate_server(server, i)

    if "namespaces" in config:
        validate_namespaces(config["namespaces"], config["servers"])

    if "groups" in config:
        validate_groups(config["groups"], config.get("namespaces", {}))

    if "manifests" in config:
        validate_manifests(config["manifests"])

    if "sandbox" in config:
        validate_sandbox(config["sandbox"])

    if "reasoning" in config:
        validate_reasoning(config["reasoning"])

    if "auth" in config:
        validate_auth(config.get("auth", {}))

    if "security" in config:
        validate_security(config.get("security", {}))


def validate_security(security: Dict[str, Any]) -> None:
    """Validate security configuration.

    Args:
        security: Security configuration dictionary

    Raises:
        ConfigError: If security validation fails
    """
    if not isinstance(security, dict):
        raise ConfigError("security must be a JSON object")

    if "blocklist_url" in security:
        url = security["blocklist_url"]
        if not isinstance(url, str):
            raise ConfigError("security.blocklist_url must be a string")
        if not url.startswith(("https://", "http://")):
            raise ConfigError("security.blocklist_url must be HTTP(S) URL")

    if "blocklist_sync_interval" in security:
        interval = security["blocklist_sync_interval"]
        if not isinstance(interval, int) or interval < 60:
            raise ConfigError("security.blocklist_sync_interval must be >= 60 seconds")
        if "blocklist_url" not in security:
            raise ConfigError(
                "security.blocklist_sync_interval requires blocklist_url to be set"
            )

    if "blocklist_fetch_timeout" in security:
        timeout = security["blocklist_fetch_timeout"]
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError("security.blocklist_fetch_timeout must be a positive number")
        if "blocklist_sync_interval" not in security:
            raise ConfigError(
                "security.blocklist_fetch_timeout requires blocklist_sync_interval to be set"
            )
        if timeout >= security["blocklist_sync_interval"]:
            raise ConfigError(
                "security.blocklist_fetch_timeout must be strictly less than "
                "blocklist_sync_interval"
            )

    if "allow_risky_servers" in security:
        if not isinstance(security["allow_risky_servers"], bool):
            raise ConfigError("security.allow_risky_servers must be boolean")

    if "risky_server_acknowledgments" in security:
        acks = security["risky_server_acknowledgments"]
        if not isinstance(acks, dict):
            raise ConfigError("security.risky_server_acknowledgments must be an object")
        for name, reason in acks.items():
            if not isinstance(reason, str) or len(reason) < 10:
                raise ConfigError(
                    f"security.risky_server_acknowledgments.{name} "
                    f"must be a descriptive string (>= 10 chars)"
                )


def validate_config_with_result(
    config: Dict[str, Any],
) -> Tuple[bool, List[str], List[str]]:
    """Validate configuration and return result with errors and warnings.

    Args:
        config: Configuration dictionary to validate

    Returns:
        Tuple of (is_valid, errors, warnings)
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(config, dict):
        return (False, ["Config must be a JSON object"], [])

    if "servers" not in config:
        return (False, ["Config missing required 'servers' field"], [])

    if not isinstance(config["servers"], list):
        return (False, ["'servers' must be an array"], [])

    for i, server in enumerate(config["servers"]):
        try:
            validate_server(server, i)
        except ConfigError as e:
            errors.append(str(e))

    namespaces = config.get("namespaces", {})
    if "namespaces" in config:
        ns_warnings = validate_namespaces(
            config["namespaces"], config["servers"], raise_on_error=False
        )
        warnings.extend(ns_warnings)

    if "groups" in config:
        group_errors, group_warnings = validate_groups(
            config["groups"], namespaces, raise_on_error=False
        )
        errors.extend(group_errors)
        warnings.extend(group_warnings)

    if "manifests" in config:
        try:
            validate_manifests(config["manifests"])
        except ConfigError as e:
            errors.append(str(e))

    if "sandbox" in config:
        try:
            validate_sandbox(config["sandbox"])
        except ConfigError as e:
            errors.append(str(e))

    if "reasoning" in config:
        try:
            validate_reasoning(config["reasoning"])
        except ConfigError as e:
            errors.append(str(e))

    return (len(errors) == 0, errors, warnings)


def validate_namespaces(
    namespaces: Dict[str, Any],
    servers: List[Dict],
    raise_on_error: bool = True,
) -> List[str]:
    """Validate namespace configuration.

    Args:
        namespaces: Namespace configuration dict
        servers: List of server configs
        raise_on_error: If True, raise ConfigError on errors; if False, return warnings

    Returns:
        When raise_on_error=False: List of warning messages
        When raise_on_error=True: Empty list (always raises on error)

    Raises:
        ConfigError: If namespace config is invalid and raise_on_error=True
    """
    warnings: List[str] = []

    if not isinstance(namespaces, dict):
        if raise_on_error:
            raise ConfigError("'namespaces' must be an object")
        return warnings

    server_names = {s["name"] for s in servers}

    for ns_name, ns_config in namespaces.items():
        if ns_config is None:
            if raise_on_error:
                raise ConfigError(f"Namespace '{ns_name}' cannot be null")
            continue

        normalized = normalize_namespace_config(ns_config)

        # Validate servers
        if isinstance(ns_config, list):
            validate_namespace_servers(ns_name, normalized["servers"], server_names)
        elif isinstance(ns_config, dict):
            if "servers" in ns_config:
                validate_namespace_servers(ns_name, normalized["servers"], server_names)
            else:
                if raise_on_error:
                    raise ConfigError(
                        f"Namespace '{ns_name}' must have a 'servers' array"
                    )
            if "isolated" in ns_config:
                if not isinstance(ns_config["isolated"], bool):
                    if raise_on_error:
                        raise ConfigError(
                            f"Namespace '{ns_name}' 'isolated' must be a boolean"
                        )
                elif ns_config.get("isolated"):
                    warnings.append(f"Namespace '{ns_name}' is marked as isolated")
            if "extends" in ns_config:
                validate_namespace_extends(ns_name, normalized["extends"], namespaces)
        else:
            if raise_on_error:
                raise ConfigError(f"Namespace '{ns_name}' must be an array or object")

    return warnings


def validate_groups(
    groups: Dict[str, Any],
    namespaces: Dict[str, Any],
    raise_on_error: bool = True,
) -> Tuple[List[str], List[str]]:
    """Validate groups configuration.

    Args:
        groups: Groups configuration dict
        namespaces: Namespace configuration dict
        raise_on_error: If True, raise ConfigError on errors; if False, return (errors, warnings)

    Returns:
        When raise_on_error=False: Tuple of (errors, warnings)
        When raise_on_error=True: Empty tuple (always raises on error)

    Raises:
        ConfigError: If groups config is invalid and raise_on_error=True
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(groups, dict):
        if raise_on_error:
            raise ConfigError("'groups' must be an object")
        errors.append("'groups' must be an object")
        return (errors, warnings) if not raise_on_error else (errors, warnings)

    for group_name, group_config in groups.items():
        if group_config is None:
            msg = f"Group '{group_name}' cannot be null"
            if raise_on_error:
                raise ConfigError(msg)
            errors.append(msg)
            continue

        if not isinstance(group_config, dict):
            msg = f"Group '{group_name}' must be an object"
            if raise_on_error:
                raise ConfigError(msg)
            errors.append(msg)
            continue

        if "namespaces" not in group_config:
            msg = f"Group '{group_name}' must have a 'namespaces' array"
            if raise_on_error:
                raise ConfigError(msg)
            errors.append(msg)
            continue

        if not isinstance(group_config["namespaces"], list):
            msg = f"Group '{group_name}' 'namespaces' must be an array"
            if raise_on_error:
                raise ConfigError(msg)
            errors.append(msg)
            continue

        for ns_ref in group_config["namespaces"]:
            if not isinstance(ns_ref, str):
                msg = (
                    f"Group '{group_name}' has non-string namespace reference: {ns_ref}"
                )
                if raise_on_error:
                    raise ConfigError(msg)
                errors.append(msg)
                continue

            has_force_prefix = ns_ref.startswith("!")
            actual_ns_name = ns_ref[1:] if has_force_prefix else ns_ref

            if actual_ns_name not in namespaces:
                msg = f"Group '{group_name}' references unknown namespace '{actual_ns_name}'"
                if raise_on_error:
                    raise ConfigError(msg)
                errors.append(msg)
                continue

            ns_def = namespaces.get(actual_ns_name)
            is_isolated = False
            if isinstance(ns_def, dict):
                is_isolated = ns_def.get("isolated", False)

            if has_force_prefix:
                dep_msg = (
                    f"Group '{group_name}' uses deprecated '!' prefix on '{actual_ns_name}' - "
                    f"list the namespace directly"
                )
                warnings.append(dep_msg)
                if raise_on_error:
                    logger.warning(dep_msg)

            if is_isolated:
                iso_msg = f"Group '{group_name}' includes isolated namespace '{actual_ns_name}'"
                warnings.append(iso_msg)
                if raise_on_error:
                    logger.warning(iso_msg)

    return (errors, warnings)


def validate_server(server: Dict[str, Any], index: int) -> None:
    """Validate a single server configuration.

    Supports both stdio (command/args) and HTTP (url) server types.
    Type is inferred from fields unless explicitly set via 'type'.

    Args:
        server: Server configuration dict
        index: Server index for error messages

    Raises:
        ConfigError: If server config is invalid
    """
    if not isinstance(server, dict):
        raise ConfigError(f"Server {index} must be an object")

    has_url = "url" in server
    has_command = "command" in server

    if has_url and has_command:
        raise ConfigError(f"Server {index} cannot have both 'url' and 'command'")

    if has_url:
        server_type = "http"
    elif has_command:
        server_type = "stdio"
    elif server.get("type") == "http":
        server_type = "http"
    else:
        server_type = server.get("type", "stdio")

    if server_type == "http":
        required_fields = ["name", "url"]
    else:
        required_fields = ["name", "command"]

    for field in required_fields:
        if field not in server:
            raise ConfigError(f"Server {index} missing required field '{field}'")

    if not isinstance(server["name"], str) or not server["name"]:
        raise ConfigError(f"Server {index} 'name' must be a non-empty string")

    if server_type != "http":
        if not isinstance(server["command"], str) or not server["command"]:
            raise ConfigError(f"Server {index} 'command' must be a non-empty string")
    else:
        if not isinstance(server.get("url", ""), str) or not server["url"]:
            raise ConfigError(
                f"Server {index} 'url' must be a non-empty string for HTTP backend"
            )
        if not server["url"].startswith(("http://", "https://")):
            raise ConfigError(f"Server {index} 'url' must be a valid HTTP(S) URL")

    if "args" in server and not isinstance(server["args"], list):
        raise ConfigError(f"Server {index} 'args' must be an array")

    if "env" in server and not isinstance(server["env"], dict):
        raise ConfigError(f"Server {index} 'env' must be an object")

    if "timeout" in server and not isinstance(server["timeout"], int):
        raise ConfigError(f"Server {index} 'timeout' must be an integer")

    if "headers" in server and not isinstance(server["headers"], dict):
        raise ConfigError(f"Server {index} 'headers' must be an object")

    if "strip_tool_prefix" in server:
        if not isinstance(server["strip_tool_prefix"], str):
            raise ConfigError(f"Server {index} 'strip_tool_prefix' must be a string")
        if not server["strip_tool_prefix"]:
            raise ConfigError(
                f"Server {index} 'strip_tool_prefix' must be a non-empty string"
            )

    if "tool_prefix" in server:
        if not isinstance(server["tool_prefix"], str):
            raise ConfigError(f"Server {index} 'tool_prefix' must be a string")

    # Warn if env is set on an HTTP server (only affects adapter process level)
    if server_type == "http" and "env" in server:
        logger.warning(
            f"Server {index} ({server['name']}): 'env' field on HTTP server has no "
            f"effect — environment variables only propagate to stdio adapters. "
            f"Use 'headers' for HTTP authentication or set env vars in the adapter "
            f"systemd unit directly."
        )


def validate_reasoning(reasoning: Dict[str, Any]) -> None:
    """Validate reasoning engine configuration.

    Args:
        reasoning: Reasoning configuration dict

    Raises:
        ConfigError: If reasoning config is invalid
    """
    if not isinstance(reasoning, dict):
        raise ConfigError("'reasoning' must be an object")

    if "default" in reasoning:
        default = reasoning["default"]
        if not isinstance(default, str):
            raise ConfigError("reasoning.default must be a string")

    if "engines" in reasoning:
        engines = reasoning["engines"]
        if not isinstance(engines, dict):
            raise ConfigError("reasoning.engines must be an object")
        for name, engine_cfg in engines.items():
            if not isinstance(engine_cfg, dict):
                raise ConfigError(
                    f"reasoning.engines.{name} must be an object"
                )
            if "server" not in engine_cfg:
                raise ConfigError(
                    f"reasoning.engines.{name} missing required 'server' field"
                )
            if not isinstance(engine_cfg["server"], str):
                raise ConfigError(
                    f"reasoning.engines.{name}.server must be a string"
                )

    if "auto_think" in reasoning:
        auto = reasoning["auto_think"]
        if not isinstance(auto, dict):
            raise ConfigError("reasoning.auto_think must be an object")
        if "enabled" in auto and not isinstance(auto["enabled"], bool):
            raise ConfigError("reasoning.auto_think.enabled must be a boolean")
        if "keywords" in auto:
            if not isinstance(auto["keywords"], list):
                raise ConfigError("reasoning.auto_think.keywords must be an array")
            for kw in auto["keywords"]:
                if not isinstance(kw, str):
                    raise ConfigError(
                        "reasoning.auto_think.keywords entries must be strings"
                    )
        if "complexity_threshold" in auto:
            threshold = auto["complexity_threshold"]
            if not isinstance(threshold, int) or threshold < 1:
                raise ConfigError(
                    "reasoning.auto_think.complexity_threshold must be >= 1"
                )


def validate_namespace_servers(
    ns_name: str, servers: List[str], all_server_names: Set[str]
) -> None:
    """Validate servers list in a namespace.

    Args:
        ns_name: Namespace name
        servers: List of server names
        all_server_names: Set of all valid server names

    Raises:
        ConfigError: If servers list is invalid
    """
    if not isinstance(servers, list):
        raise ConfigError(f"Namespace '{ns_name}' servers must be an array")

    for server_name in servers:
        if not isinstance(server_name, str):
            raise ConfigError(
                f"Namespace '{ns_name}' has non-string server name: {server_name}"
            )
        if server_name not in all_server_names:
            raise ConfigError(
                f"Namespace '{ns_name}' references unknown server '{server_name}'"
            )


def validate_namespace_extends(
    ns_name: str, extends: Any, namespaces: Dict[str, Any]
) -> None:
    """Validate extends field in a namespace.

    Args:
        ns_name: Namespace name
        extends: Value of extends field
        namespaces: All namespace configurations

    Raises:
        ConfigError: If extends is invalid
    """
    if extends is None:
        return

    if isinstance(extends, str):
        extends = [extends]
    elif not isinstance(extends, list):
        raise ConfigError(
            f"Namespace '{ns_name}' extends must be a string, array, or null"
        )

    for parent in extends:
        if not isinstance(parent, str):
            raise ConfigError(
                f"Namespace '{ns_name}' extends has non-string value: {parent}"
            )
        if parent not in namespaces:
            raise ConfigError(
                f"Namespace '{ns_name}' extends unknown namespace '{parent}'"
            )


def validate_manifests(manifests: Dict[str, Any]) -> None:
    """Validate manifests configuration.

    Args:
        manifests: Manifests configuration dict

    Raises:
        ConfigError: If manifests config is invalid
    """
    if not isinstance(manifests, dict):
        raise ConfigError("'manifests' must be an object")

    if "startup_dwell_secs" in manifests:
        if not isinstance(manifests["startup_dwell_secs"], (int, float)):
            raise ConfigError("manifests.startup_dwell_secs must be a number")
        if manifests["startup_dwell_secs"] < 0:
            raise ConfigError("manifests.startup_dwell_secs must be non-negative")

    if "per_server_ttl" in manifests:
        ttl = manifests["per_server_ttl"]
        if isinstance(ttl, dict):
            if "default_secs" in ttl and not isinstance(
                ttl["default_secs"], (int, float)
            ):
                raise ConfigError("per_server_ttl.default_secs must be a number")
        elif ttl is not None and not isinstance(ttl, (int, float)):
            raise ConfigError("per_server_ttl must be a number, object, or null")


def validate_sandbox(sandbox: Dict[str, Any]) -> None:
    """Validate sandbox configuration.

    Args:
        sandbox: Sandbox configuration dict

    Raises:
        ConfigError: If sandbox config is invalid
    """
    if not isinstance(sandbox, dict):
        raise ConfigError("'sandbox' must be an object")

    if "timeout_secs" in sandbox:
        if not isinstance(sandbox["timeout_secs"], int):
            raise ConfigError("sandbox.timeout_secs must be an integer")
        if sandbox["timeout_secs"] < 1:
            raise ConfigError("sandbox.timeout_secs must be at least 1")

    if "memory_mb" in sandbox:
        if not isinstance(sandbox["memory_mb"], int):
            raise ConfigError("sandbox.memory_mb must be an integer")
        if sandbox["memory_mb"] < 1:
            raise ConfigError("sandbox.memory_mb must be at least 1")


def validate_auth(auth: Dict[str, Any]) -> None:
    """Validate auth configuration.

    Args:
        auth: Auth configuration dict

    Raises:
        ConfigError: If auth config is invalid
    """
    if not isinstance(auth, dict):
        raise ConfigError("'auth' must be an object")

    if "enabled" in auth:
        if not isinstance(auth["enabled"], bool):
            raise ConfigError("auth.enabled must be a boolean")

    if "jwt" in auth:
        pass  # JWT config validation removed - handled by auth module

    if "credentials" in auth:
        if not isinstance(auth["credentials"], dict):
            raise ConfigError("auth.credentials must be an object")

    if "scopes" in auth:
        if not isinstance(auth["scopes"], dict):
            raise ConfigError("auth.scopes must be an object")

    if "tool_scopes" in auth:
        if not isinstance(auth["tool_scopes"], dict):
            raise ConfigError("auth.tool_scopes must be an object")

    if "admin_key_env" in auth:
        if not isinstance(auth["admin_key_env"], str):
            raise ConfigError("auth.admin_key_env must be a string")

    if "rotate_reauth" in auth:
        if not isinstance(auth["rotate_reauth"], bool):
            raise ConfigError("auth.rotate_reauth must be a boolean")


def interpolate_env_vars(config: Dict[str, Any]) -> Dict[str, Any]:
    """Interpolate environment variables in config values.

    Replaces ${VAR_NAME} with the value of the environment variable.

    Args:
        config: Configuration dictionary

    Returns:
        Config with interpolated environment variables
    """
    env_pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def interpolate(value: Any) -> Any:
        if isinstance(value, str):

            def replace_var(match: re.Match) -> str:
                var_name = match.group(1)
                var_value = os.environ.get(var_name)
                if var_value is None:
                    logger.warning(
                        f"Environment variable {var_name} not found, using empty string"
                    )
                    return ""
                return var_value

            return env_pattern.sub(replace_var, value)
        elif isinstance(value, dict):
            return {k: interpolate(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [interpolate(v) for v in value]
        return value

    return interpolate(config)


def allocate_port(config: Dict[str, Any], base_port: int = 12020) -> int:
    """Find next available port not used by any configured HTTP server.

    Scans server configs for ports parsed from 'url' fields and returns
    the first port >= base_port that is not in use.

    Args:
        config: Configuration dictionary with 'servers' list
        base_port: Starting port to search from

    Returns:
        Next available port number
    """
    used_ports: set[int] = set()
    for server in config.get("servers", []):
        url = server.get("url", "")
        if url and isinstance(url, str):
            try:
                parsed = urlparse(url)
                if parsed.port is not None:
                    used_ports.add(parsed.port)
            except Exception:
                pass

    port = base_port
    while port in used_ports:
        port += 1
    return port
