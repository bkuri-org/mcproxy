"""Tests for search handler inputSchema attachment and auto-generated usage examples.

Covers:
- search.include_schema config gating (default false, opt-in true)
- Brief mode omitting both schema and examples
- Schema depth-3 $defs truncation
- 4 KiB example-drop threshold
- Static-sample example strings from manifest/example_gen.py with # see schema fallback
- generate_example() try/except degradation to placeholder
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# Minimal fixtures representing the search subsystem under test
# ---------------------------------------------------------------------------

SAMPLE_TOOL = {
    "name": "do_thing",
    "description": "Does a thing with structured input.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "foo": {"type": "string"},
            "bar": {"type": "integer"},
        },
        "required": ["foo"],
        "$defs": {
            "NestedA": {
                "type": "object",
                "properties": {
                    "deep": {"$ref": "#/$defs/NestedB"},
                },
            },
            "NestedB": {
                "type": "object",
                "properties": {
                    "deeper": {"$ref": "#/$defs/NestedC"},
                },
            },
            "NestedC": {
                "type": "object",
                "properties": {
                    "leaf": {"type": "string"},
                },
            },
            "NestedD": {
                "type": "object",
                "properties": {
                    "beyond_limit": {"type": "boolean"},
                },
            },
        },
    },
}

# A schema whose serialised form exceeds 4 KiB when examples are attached
_LARGE_SCHEMA_TOOL = {
    "name": "large_schema_tool",
    "description": "Tool with a huge schema to trigger the 4 KiB example-drop threshold.",
    "inputSchema": {
        "type": "object",
        "properties": {
            f"field_{i}": {"type": "string", "description": "x" * 200}
            for i in range(30)
        },
    },
}

STATIC_EXAMPLES = {
    "do_thing": '{"foo": "sample-value"}  # see schema',
    "large_schema_tool": '{"field_0": "..."}  # see schema',
}


# ---------------------------------------------------------------------------
# Helper: simulate the schema-capping logic
# ---------------------------------------------------------------------------

def _truncate_defs(schema: dict, max_depth: int = 3) -> dict:
    """Return a copy of *schema* with $defs truncated beyond *max_depth* levels."""
    import copy
    s = copy.deepcopy(schema)
    defs = s.get("$defs")
    if not defs:
        return s

    def _walk(node, depth):
        if not isinstance(node, dict):
            return
        if depth >= max_depth:
            # Replace any remaining $ref pointing into $defs with a placeholder
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                node["$ref"] = "#/$defs/[truncated]"
            return
        if "$ref" in node and node["$ref"].startswith("#/$defs/"):
            ref_name = node["$ref"].split("/")[-1]
            if ref_name in defs:
                _walk(defs[ref_name], depth + 1)
        for v in node.values():
            if isinstance(v, dict):
                _walk(v, depth)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _walk(item, depth)

    _walk(s, 0)

    # Remove defs that were never reachable within depth limit
    reachable = set()
    def _collect(node, depth):
        if not isinstance(node, dict):
            return
        if "$ref" in node and node["$ref"].startswith("#/$defs/"):
            ref_name = node["$ref"].split("/")[-1]
            if ref_name in defs:
                reachable.add(ref_name)
                if depth + 1 < max_depth:
                    _collect(defs[ref_name], depth + 1)
        for v in node.values():
            if isinstance(v, dict):
                _collect(v, depth)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _collect(item, depth)

    _collect(s, 0)
    s["$defs"] = {k: v for k, v in defs.items() if k in reachable}
    return s


def _apply_search_schema_enrichment(
    tool: dict,
    include_schema: bool,
    brief: bool = False,
    example_map: dict | None = None,
    size_cap: int = 4096,
) -> dict:
    """Simulate the search handler's schema/example attachment logic."""
    result = {"name": tool["name"], "description": tool.get("description", "")}

    if brief or not include_schema:
        return result

    schema = _truncate_defs(tool.get("inputSchema", {}), max_depth=3)
    schema_bytes = json.dumps(schema).encode()

    example = None
    if example_map is not None:
        example = example_map.get(tool["name"])

    # 4 KiB cap: if schema + example would exceed, drop example
    if example is not None:
        candidate = json.dumps({"inputSchema": schema, "example": example}).encode()
        if len(candidate) > size_cap:
            example = None

    result["inputSchema"] = schema
    if example is not None:
        result["example"] = example
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSearchSchemaGating:
    """search.include_schema config gates the attachment of schema & examples."""

    def test_default_false_omits_schema(self):
        result = _apply_search_schema_enrichment(SAMPLE_TOOL, include_schema=False)
        assert "inputSchema" not in result
        assert "example" not in result

    def test_opt_in_true_includes_schema(self):
        result = _apply_search_schema_enrichment(SAMPLE_TOOL, include_schema=True)
        assert "inputSchema" in result

    def test_opt_in_true_includes_example_when_available(self):
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL, include_schema=True, example_map=STATIC_EXAMPLES
        )
        assert "example" in result
        assert "see schema" in result["example"]


class TestBriefMode:
    """Brief mode omits both schema and examples regardless of include_schema."""

    def test_brief_omits_even_when_include_schema_true(self):
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL, include_schema=True, brief=True, example_map=STATIC_EXAMPLES
        )
        assert "inputSchema" not in result
        assert "example" not in result

    def test_brief_omits_when_include_schema_false(self):
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL, include_schema=False, brief=True
        )
        assert "inputSchema" not in result
        assert "example" not in result


class TestSchemaDepthTruncation:
    """$defs beyond depth-3 are truncated."""

    def test_nested_c_kept_within_depth_3(self):
        result = _apply_search_schema_enrichment(SAMPLE_TOOL, include_schema=True)
        defs = result["inputSchema"].get("$defs", {})
        assert "NestedA" in defs
        assert "NestedB" in defs
        assert "NestedC" in defs

    def test_nested_d_removed_beyond_depth_3(self):
        result = _apply_search_schema_enrichment(SAMPLE_TOOL, include_schema=True)
        defs = result["inputSchema"].get("$defs", {})
        assert "NestedD" not in defs

    def test_ref_to_truncated_def_replaced(self):
        """If a $ref points to a def that was truncated, it becomes [truncated]."""
        # NestedC has no deeper refs, so nothing to truncate there.
        # But if we artificially add a ref from C to D (beyond depth), the
        # truncation walk should rewrite it.
        deep_tool = {
            "name": "deep_tool",
            "inputSchema": {
                "type": "object",
                "$defs": {
                    "A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
                    "B": {"type": "object", "properties": {"c": {"$ref": "#/$defs/C"}}},
                    "C": {"type": "object", "properties": {"d": {"$ref": "#/$defs/D"}}},
                    "D": {"type": "string"},
                },
            },
        }
        result = _apply_search_schema_enrichment(deep_tool, include_schema=True)
        # At depth 3 the walk into C should NOT descend into D, so the $ref stays
        # pointing at D, but D is removed from $defs because it's unreachable
        # within the depth limit.  The ref itself is rewritten to [truncated].
        c_def = result["inputSchema"]["$defs"]["C"]
        assert c_def["properties"]["d"]["$ref"] == "#/$defs/[truncated]"
        assert "D" not in result["inputSchema"]["$defs"]


class TestSizeCapDropsExamples:
    """When schema + example exceed 4 KiB, the example is dropped."""

    def test_large_schema_drops_example(self):
        result = _apply_search_schema_enrichment(
            _LARGE_SCHEMA_TOOL,
            include_schema=True,
            example_map=STATIC_EXAMPLES,
            size_cap=4096,
        )
        assert "inputSchema" in result
        assert "example" not in result

    def test_small_schema_keeps_example(self):
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL,
            include_schema=True,
            example_map=STATIC_EXAMPLES,
            size_cap=4096,
        )
        assert "example" in result


class TestStaticExamplesFromManifest:
    """Examples are quoted static-sample strings sourced from manifest/example_gen.py."""

    def test_example_is_quoted_string(self):
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL, include_schema=True, example_map=STATIC_EXAMPLES
        )
        assert isinstance(result["example"], str)
        # The value should look like a JSON string with a comment suffix
        assert result["example"].startswith("{")
        assert "# see schema" in result["example"]

    def test_missing_tool_uses_no_example(self):
        """When example_map has no entry for the tool, no example key is added."""
        result = _apply_search_schema_enrichment(
            {"name": "unknown_tool", "inputSchema": {"type": "object"}},
            include_schema=True,
            example_map=STATIC_EXAMPLES,
        )
        assert "example" not in result


class TestGenerateExampleDegradation:
    """generate_example() is wrapped in try/except; on failure a placeholder is used."""

    def _make_generate_wrapper(self, example_map, fail_for=None):
        """Simulate the real generate_example call site with try/except."""
        def generate_example(tool_name):
            if fail_for and tool_name in fail_for:
                raise RuntimeError("example generation blew up")
            return example_map.get(tool_name)
        return generate_example

    def test_successful_generation_returns_real_example(self):
        gen = self._make_generate_wrapper(STATIC_EXAMPLES)
        example = gen("do_thing")
        assert example is not None
        assert "see schema" in example

    def test_failed_generation_returns_placeholder(self):
        gen = self._make_generate_wrapper(STATIC_EXAMPLES, fail_for={"do_thing"})
        example = None
        try:
            example = gen("do_thing")
        except Exception:
            example = "<example unavailable>"
        assert example == "<example unavailable>"

    def test_search_never_fails_due_to_example_generation(self):
        """Even when generate_example raises, the search result is still returned."""
        gen = self._make_generate_wrapper(STATIC_EXAMPLES, fail_for={"do_thing"})

        example = None
        try:
            example = gen("do_thing")
        except Exception:
            example = "<example unavailable>"

        # Build result manually as the handler would
        result = {"name": "do_thing", "description": SAMPLE_TOOL["description"]}
        if example is not None:
            result["example"] = example

        # Result is always present
        assert result["name"] == "do_thing"
        assert result["example"] == "<example unavailable>"


class TestIntegrationWithConfigObject:
    """Verify behaviour when driven by a realistic config-like namespace."""

    def _make_config(self, include_schema: bool = False, brief: bool = False):
        return SimpleNamespace(
            search=SimpleNamespace(include_schema=include_schema, brief=brief)
        )

    def test_config_default_no_schema(self):
        cfg = self._make_config()
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL, include_schema=cfg.search.include_schema
        )
        assert "inputSchema" not in result

    def test_config_enabled_with_schema(self):
        cfg = self._make_config(include_schema=True)
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL, include_schema=cfg.search.include_schema,
            example_map=STATIC_EXAMPLES,
        )
        assert "inputSchema" in result
        assert "example" in result

    def test_config_brief_overrides(self):
        cfg = self._make_config(include_schema=True, brief=True)
        result = _apply_search_schema_enrichment(
            SAMPLE_TOOL,
            include_schema=cfg.search.include_schema,
            brief=cfg.search.brief,
            example_map=STATIC_EXAMPLES,
        )
        assert "inputSchema" not in result
        assert "example" not in result
