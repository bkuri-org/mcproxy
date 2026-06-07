"""Tests for utils/param_normalize.py — snake_case/camelCase parameter normalization."""

import pytest
from utils.param_normalize import (
    detect_convention,
    to_snake_case,
    to_camel_case,
    build_param_map,
    normalize_params,
    clear_cache,
)


class TestDetectConvention:
    """Convention detection from parameter names."""

    def test_snake_case(self):
        names = {"thought_number", "total_thoughts", "next_thought_needed"}
        assert detect_convention(names) == "snake_case"

    def test_camel_case(self):
        names = {"thoughtNumber", "totalThoughts", "nextThoughtNeeded"}
        assert detect_convention(names) == "camelCase"

    def test_mixed(self):
        names = {"thought_number", "totalThoughts", "foo_bar"}
        assert detect_convention(names) is None

    def test_too_few(self):
        assert detect_convention({"name"}) is None
        assert detect_convention(set()) is None


class TestConversions:
    """Bidirectional name conversion."""

    def test_to_snake_case(self):
        assert to_snake_case("thoughtNumber") == "thought_number"
        assert to_snake_case("nextThoughtNeeded") == "next_thought_needed"
        assert to_snake_case("HTTPResponse") == "http_response"

    def test_to_camel_case(self):
        assert to_camel_case("thought_number") == "thoughtNumber"
        assert to_camel_case("next_thought_needed") == "nextThoughtNeeded"
        assert to_camel_case("total_thoughts") == "totalThoughts"


class TestBuildParamMap:
    """Building normalization maps between incoming and schema params."""

    def test_camel_to_snake(self):
        schema_props = {
            "thought_number": {"type": "integer"},
            "thought": {"type": "string"},
            "next_thought_needed": {"type": "boolean"},
        }
        incoming = {"thoughtNumber": 1, "thought": "test", "nextThoughtNeeded": True}
        mapping = build_param_map(schema_props, incoming)
        assert mapping["thoughtNumber"] == "thought_number"
        assert mapping["nextThoughtNeeded"] == "next_thought_needed"
        assert "thought" not in mapping  # exact match

    def test_snake_to_camel(self):
        schema_props = {
            "userName": {"type": "string"},
            "emailAddress": {"type": "string"},
        }
        incoming = {"user_name": "alice", "email_address": "a@b.com"}
        mapping = build_param_map(schema_props, incoming)
        assert mapping["user_name"] == "userName"
        assert mapping["email_address"] == "emailAddress"

    def test_no_normalization_needed(self):
        schema_props = {"query": {"type": "string"}, "limit": {"type": "integer"}}
        incoming = {"query": "test", "limit": 5}
        mapping = build_param_map(schema_props, incoming)
        assert mapping == {}

    def test_ambiguous_returns_empty(self):
        schema_props = {"foo_bar": {"type": "string"}, "bazQux": {"type": "integer"}}
        incoming = {"fooBar": "val", "baz_qux": 42}
        mapping = build_param_map(schema_props, incoming)
        assert mapping == {}  # Mixed convention — skip


class TestNormalizeParams:
    """End-to-end parameter normalization."""

    def setup_method(self):
        clear_cache()

    def test_camel_to_snake_incoming(self):
        schema = {
            "properties": {
                "thought_number": {"type": "integer"},
                "thought": {"type": "string"},
                "next_thought_needed": {"type": "boolean"},
            },
            "required": ["thought", "thought_number", "next_thought_needed"],
        }
        args = {"thoughtNumber": 1, "thought": "test", "nextThoughtNeeded": True}
        result = normalize_params("think", "think_tool", args, schema)
        assert result == {"thought": "test", "thought_number": 1, "next_thought_needed": True}

    def test_snake_to_camel_incoming(self):
        schema = {
            "properties": {
                "userName": {"type": "string"},
                "emailAddress": {"type": "string"},
            },
            "required": ["userName", "emailAddress"],
        }
        args = {"user_name": "alice", "email_address": "a@b.com"}
        result = normalize_params("test", "get_user", args, schema)
        assert result == {"userName": "alice", "emailAddress": "a@b.com"}

    def test_already_correct(self):
        schema = {
            "properties": {"thought_number": {"type": "integer"}},
        }
        args = {"thought_number": 1}
        result = normalize_params("test", "tool", args, schema)
        assert result == {"thought_number": 1}
        assert result is args  # Same object reference = no copy

    def test_convention_cached(self):
        """Convention detection should be cached per (server, tool)."""
        from utils.param_normalize import _convention_cache

        clear_cache()
        schema = {
            "properties": {
                "thought_number": {"type": "integer"},
                "next_thought_needed": {"type": "boolean"},
            },
        }

        # First call populates cache
        normalize_params("srv", "tool", {"thoughtNumber": 1}, schema)
        assert ("srv", "tool") in _convention_cache
        assert _convention_cache[("srv", "tool")] == "snake_case"

        # Different server gets separate cache entry
        normalize_params("other", "tool", {"thoughtNumber": 1}, schema)
        assert ("other", "tool") in _convention_cache
        assert ("srv", "tool") in _convention_cache

    def test_empty_args(self):
        assert normalize_params("t", "t", {}, {"properties": {"a": {"type": "string"}}}) == {}

    def test_none_schema(self):
        assert normalize_params("t", "t", {"a": 1}, None) == {"a": 1}