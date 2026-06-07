"""Tests for manifest/example_gen.py — usage example generation from JSON Schema."""

import pytest
from manifest.example_gen import (
    generate_tool_example,
    _param_example,
    _format_value,
    _PLACEHOLDER_TEMPLATES,
)


class TestParamExample:
    """Parameter-level example generation."""

    def test_placeholder_string(self):
        result = _param_example("query", {"type": "string"})
        assert result == "'<query>'"

    def test_placeholder_boolean(self):
        result = _param_example("enabled", {"type": "boolean"})
        assert result == "true"

    def test_placeholder_integer(self):
        result = _param_example("limit", {"type": "integer"})
        # limit is in _PLACEHOLDER_TEMPLATES with value 5
        assert result == "5"

    def test_enum_first_value(self):
        result = _param_example("model", {"type": "string", "enum": ["sonar", "sonar-pro"]})
        assert result == "'sonar'"

    def test_unknown_param(self):
        result = _param_example("some_random_field", {"type": "string"})
        assert result == "'<some_random_field>'"


class TestFormatValue:
    """Value formatting for examples."""

    def test_string(self):
        assert _format_value("hello") == "'hello'"

    def test_int(self):
        assert _format_value(42) == "42"

    def test_bool(self):
        assert _format_value(True) == "true"
        assert _format_value(False) == "false"

    def test_list(self):
        assert _format_value([1, 2, 3]) == "[1, 2, 3]"


class TestGenerateToolExample:
    """Full tool example generation."""

    def test_simple_required_string(self):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        result = generate_tool_example("wikipedia", "search", schema)
        assert result == "api.server('wikipedia').search(query='<query>')"

    def test_required_and_optional(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        }
        result = generate_tool_example("wikipedia", "search", schema)
        assert result.startswith("api.server('wikipedia').search(query='<query>'")
        assert "..." in result  # Optional params truncated

    def test_boolean_param(self):
        schema = {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "next_thought_needed": {"type": "boolean"},
            },
            "required": ["thought", "next_thought_needed"],
        }
        result = generate_tool_example("sequential_thinking", "think", schema)
        assert "next_thought_needed=true" in result

    def test_enum_param(self):
        schema = {
            "type": "object",
            "properties": {"model": {"type": "string", "enum": ["sonar", "sonar-pro"]}},
            "required": ["model"],
        }
        result = generate_tool_example("perplexity_sonar", "chat", schema)
        assert "model='sonar'" in result

    def test_no_params(self):
        schema = {"type": "object", "properties": {}}
        result = generate_tool_example("test", "ping", schema)
        assert result == "api.server('test').ping()"

    def test_no_schema(self):
        result = generate_tool_example("test", "noop", None)
        assert result == "api.server('test').noop(...)"

    def test_empty_schema_object(self):
        schema = {"type": "object", "properties": {}}
        result = generate_tool_example("test", "noop", schema)
        assert result == "api.server('test').noop()"

    def test_empty_dict_schema(self):
        result = generate_tool_example("test", "noop", {})
        assert result == "api.server('test').noop()"

    def test_many_params_truncated(self):
        properties = {}
        for i in range(15):
            properties[f"param_{i}"] = {"type": "string"}
        schema = {"type": "object", "properties": properties, "required": []}
        result = generate_tool_example("test", "big", schema)
        # Should include ... hint when truncated
        assert "..." in result
        assert len(result) <= 200  # Check reasonable length

    def test_skip_context_params(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "context": {"type": "object"},
                "metadata": {"type": "object"},
            },
            "required": ["query"],
        }
        result = generate_tool_example("test", "tool", schema)
        assert "context" not in result
        assert "metadata" not in result
        assert "query" in result