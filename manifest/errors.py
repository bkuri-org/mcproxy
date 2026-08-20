"""Exceptions and validation for manifest system."""

from typing import Any, Dict, List


class ManifestError(Exception):
    """Error in manifest operations."""

    pass


class NamespaceInheritanceError(ManifestError):
    """Error in namespace inheritance resolution."""

    pass


def validate_group(
    name: str, group_def: dict, namespaces: dict
) -> tuple[bool, list[str]]:
    """Validate a group definition.

    Args:
        name: Group name
        group_def: Group definition with 'namespaces' key
        namespaces: All namespace definitions

    Returns:
        (is_valid, warnings)
    """
    warnings: list[str] = []
    is_valid = True

    ns_refs = group_def.get("namespaces", [])
    if not ns_refs:
        warnings.append(f"Group '{name}' has no namespaces defined")
        return False, warnings

    for ns_ref in ns_refs:
        explicit_isolated = ns_ref.startswith("!")
        actual_name = ns_ref[1:] if explicit_isolated else ns_ref

        if actual_name not in namespaces:
            warnings.append(
                f"Group '{name}' references unknown namespace '{actual_name}'"
            )
            is_valid = False
            continue

        if explicit_isolated:
            warnings.append(
                f"Group '{name}' uses deprecated '!' prefix on '{actual_name}' - "
                f"list the namespace directly"
            )

        ns_def = namespaces.get(actual_name, {})
        is_isolated = (
            ns_def.get("isolated", False) if isinstance(ns_def, dict) else False
        )

        if is_isolated:
            warnings.append(
                f"Group '{name}' includes isolated namespace '{actual_name}'"
            )

    return is_valid, warnings
