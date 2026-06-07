"""Tests for human-friendly validation error formatting and parameter enrichment.

Covers:
- _format_upstream_error() in http_backend.py (Part A)
- _enrich_param_error() in server_manager.py (Part B)
- _build_param_error_data() in server/handlers/tools/router.py (Part C)
- Fuzzy param name suggestions (Part C)
"""

import pytest

from http_backend import _format_upstream_error
from server_manager import _enrich_param_error, _get_tool_schema


# ============================================================================
# Part A: _format_upstream_error (http_backend.py)
# ============================================================================


class TestFormatUpstreamError:
    """Tests for the upstream error formatter."""

    def test_pydantic_missing_required(self):
        """Missing required parameter produces clear message."""
        error_details = {
            "code": -32602,
            "message": "Validation error",
            "data": [
                {
                    "type": "missing",
                    "loc": ["body", "path"],
                    "msg": "field required",
                    "input": None,
                }
            ],
        }
        result = _format_upstream_error("read_file", error_details)
        assert "Missing required parameter 'path'" in result

    def test_pydantic_extra_forbidden(self):
        """Unknown parameter produces clear message."""
        error_details = {
            "code": -32602,
            "message": "Validation error",
            "data": [
                {
                    "type": "extra_forbidden",
                    "loc": ["body", "filepath"],
                    "msg": "extra inputs not permitted",
                    "input": {"filepath": "/tmp/test"},
                }
            ],
        }
        result = _format_upstream_error("read_file", error_details)
        assert "Unknown parameter 'filepath'" in result

    def test_pydantic_type_mismatch(self):
        """Type mismatch produces parameter error message."""
        error_details = {
            "code": -32602,
            "message": "Validation error",
            "data": [
                {
                    "type": "json_type",
                    "loc": ["body", "count"],
                    "msg": "Input should be a valid integer",
                    "input": "abc",
                }
            ],
        }
        result = _format_upstream_error("search", error_details)
        assert "count" in result
        assert "invalid value" in result

    def test_pydantic_multiple_errors(self):
        """Multiple Pydantic errors are joined."""
        error_details = {
            "code": -32602,
            "message": "Validation error",
            "data": [
                {
                    "type": "missing",
                    "loc": ["body", "path"],
                    "msg": "field required",
                    "input": None,
                },
                {
                    "type": "extra_forbidden",
                    "loc": ["body", "filepath"],
                    "msg": "extra inputs not permitted",
                    "input": None,
                },
            ],
        }
        result = _format_upstream_error("read_file", error_details)
        assert "Missing required parameter 'path'" in result
        assert "Unknown parameter 'filepath'" in result

    def test_non_pydantic_error_uses_message(self):
        """Non-Pydantic errors fall back to the message field."""
        error_details = {
            "code": -32000,
            "message": "Internal server error: database connection lost",
        }
        result = _format_upstream_error("query_db", error_details)
        assert result == "Internal server error: database connection lost"

    def test_empty_data_uses_message(self):
        """Empty data list falls back to message."""
        error_details = {
            "code": -32602,
            "message": "Something went wrong",
            "data": [],
        }
        result = _format_upstream_error("tool", error_details)
        assert result == "Something went wrong"

    def test_non_list_data_uses_message(self):
        """Non-list data falls back to message."""
        error_details = {
            "code": -32602,
            "message": "Error string",
            "data": "some string data",
        }
        result = _format_upstream_error("tool", error_details)
        assert result == "Error string"

    def test_value_error_missing(self):
        """value_error.missing type produces missing message."""
        error_details = {
            "code": -32602,
            "message": "Validation error",
            "data": [
                {
                    "type": "value_error.missing",
                    "loc": ["body", "url"],
                    "msg": "Value error, missing required argument",
                    "input": None,
                }
            ],
        }
        result = _format_upstream_error("fetch", error_details)
        assert "Missing required parameter 'url'" in result

    def test_missing_loc_falls_back_to_msg(self):
        """If loc is empty, falls back to raw msg."""
        error_details = {
            "code": -32602,
            "message": "Validation error",
            "data": [
                {
                    "type": "json_type",
                    "loc": [],
                    "msg": "Invalid input",
                    "input": None,
                }
            ],
        }
        result = _format_upstream_error("tool", error_details)
        assert result == "Invalid input"


# ============================================================================
# Part B: _enrich_param_error (server_manager.py)
# ============================================================================


SAMPLE_TOOLS = [
    {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results"},
                "offset": {"type": "integer", "description": "Result offset"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "encoding": {"type": "string", "description": "File encoding"},
            },
            "required": ["path"],
        },
    },
]


class TestEnrichParamError:
    """Tests for the parameter error enricher."""

    def test_unknown_param_with_suggestion(self):
        """Unknown parameter error gets fuzzy suggestion."""
        error_msg = "Tool call failed: Unknown parameter 'quey'"
        result = _enrich_param_error("search", "test_server", error_msg, SAMPLE_TOOLS)
        assert "Did you mean 'query'?" in result
        assert "Required parameters" in result

    def test_missing_param_with_schema_info(self):
        """Missing parameter error gets available params."""
        error_msg = "Tool call failed: Missing required parameter 'path'"
        result = _enrich_param_error("read_file", "test_server", error_msg, SAMPLE_TOOLS)
        assert "Required parameters" in result
        assert "'path'" in result

    def test_non_param_error_passes_through(self):
        """Non-parameter errors are returned unchanged."""
        error_msg = "Tool call failed: Internal server error"
        result = _enrich_param_error("search", "test_server", error_msg, SAMPLE_TOOLS)
        assert result == error_msg

    def test_tool_not_found_no_enrichment(self):
        """Unknown tool name doesn't cause enrichment."""
        error_msg = "Tool call failed: Missing required parameter 'path'"
        result = _enrich_param_error(
            "nonexistent", "test_server", error_msg, SAMPLE_TOOLS
        )
        # Should still return the original message since no schema found
        assert result == error_msg

    def test_empty_tools_no_enrichment(self):
        """No tools means no enrichment."""
        error_msg = "Tool call failed: Missing required parameter 'path'"
        result = _enrich_param_error("search", "test_server", error_msg, [])
        assert result == error_msg

    def test_no_bad_param_shows_available(self):
        """Param error without extracted name shows available params."""
        error_msg = "Tool call failed: field required"
        result = _enrich_param_error("search", "test_server", error_msg, SAMPLE_TOOLS)
        assert "Required parameters" in result
        assert "'query'" in result


# ============================================================================
# Part B: _get_tool_schema (server_manager.py)
# ============================================================================


class TestGetToolSchema:
    def test_finds_existing_tool(self):
        schema = _get_tool_schema("test", "search", SAMPLE_TOOLS)
        assert schema is not None
        assert "properties" in schema
        assert "query" in schema["properties"]

    def test_returns_none_for_missing_tool(self):
        schema = _get_tool_schema("test", "nonexistent", SAMPLE_TOOLS)
        assert schema is None

    def test_returns_none_for_empty_tools(self):
        schema = _get_tool_schema("test", "search", [])
        assert schema is None


# ============================================================================
# Part C: _build_param_error_data (server/handlers/tools/router.py)
# ============================================================================


class TestBuildParamErrorData:
    def test_returns_data_for_param_error(self):
        """Param errors get enriched data dict."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["query"],
                        }
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "search",
            "Tool call failed: Unknown parameter 'quey'",
            registry,
        )
        assert data is not None
        assert "available_parameters" in data
        assert "required_parameters" in data
        assert "query" in data["available_parameters"]
        assert "query" in data["required_parameters"]

    def test_returns_none_for_non_param_error(self):
        """Non-param errors return None."""
        from server.handlers.tools.router import _build_param_error_data

        data = _build_param_error_data(
            "test_server", "search",
            "Internal server error",
            None,
        )
        assert data is None

    def test_returns_none_without_registry(self):
        """No registry means no enrichment."""
        from server.handlers.tools.router import _build_param_error_data

        data = _build_param_error_data(
            "test_server", "search",
            "Missing required parameter 'path'",
            None,
        )
        assert data is None

    def test_includes_suggestion_for_typos(self):
        """Fuzzy match produces suggestion for parameter typos."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                                "offset": {"type": "integer"},
                            },
                            "required": ["query"],
                        }
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "search",
            "Tool call failed: Unknown parameter 'limt'",
            registry,
        )
        assert data is not None
        assert "suggestion" in data
        assert "limit" in data["suggestion"]


# ============================================================================
# Part C: _extract_bad_param (server/handlers/tools/router.py)
# ============================================================================


class TestExtractBadParam:
    def test_single_quoted(self):
        from server.handlers.tools.router import _extract_bad_param

        assert _extract_bad_param("Unknown parameter 'filepath'") == "filepath"

    def test_double_quoted(self):
        from server.handlers.tools.router import _extract_bad_param

        assert _extract_bad_param('Unknown parameter "filepath"') == "filepath"

    def test_missing_param(self):
        from server.handlers.tools.router import _extract_bad_param

        assert _extract_bad_param("Missing required parameter 'path'") == "path"

    def test_no_param_found(self):
        from server.handlers.tools.router import _extract_bad_param

        assert _extract_bad_param("field required") is None


# ============================================================================
# Part D: Auto-inspect enrichment (MCPROXY-ibi)
# ============================================================================


class TestAutoInspectInRouter:
    """Tests for _build_param_error_data with auto-inspect fields."""

    def test_includes_tool_name(self):
        """Error data includes server__tool formatted name."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "read_file",
                        "description": "Read file contents",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "read_file",
            "Missing required parameter 'path'",
            registry,
        )
        assert data is not None
        assert data["tool_name"] == "test_server__read_file"

    def test_includes_input_schema(self):
        """Error data includes full inputSchema."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "search",
                        "description": "Search things",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "search",
            "Unknown parameter 'quey'",
            registry,
        )
        assert data is not None
        assert "inputSchema" in data
        assert data["inputSchema"]["properties"]["query"] is not None

    def test_includes_description(self):
        """Error data includes tool description."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "search",
                        "description": "Search things",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "search",
            "Unknown parameter 'quey'",
            registry,
        )
        assert data is not None
        assert data["description"] == "Search things"

    def test_includes_usage_example(self):
        """Error data includes generated usage example."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "search",
                        "description": "Search things",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "search",
            "Unknown parameter 'quey'",
            registry,
        )
        assert data is not None
        assert "usage_example" in data
        assert "api.server('test_server').search" in data["usage_example"]
        assert "query" in data["usage_example"]

    def test_empty_description_returns_empty_string(self):
        """Empty description does not cause errors."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {
                        "name": "toggle",
                        "description": "",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "entity_id": {"type": "string"},
                            },
                            "required": ["entity_id"],
                        },
                    }
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "toggle",
            "Missing required parameter 'entity_id'",
            registry,
        )
        assert data is not None
        assert data["description"] == ""
        assert "toggle" in data["usage_example"]

    def test_no_schema_returns_none(self):
        """Tool without inputSchema returns None."""
        from server.handlers.tools.router import _build_param_error_data
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "test_server": [
                    {"name": "noop", "description": "Does nothing"},
                ]
            }
        }

        data = _build_param_error_data(
            "test_server", "noop",
            "Missing required parameter 'x'",
            registry,
        )
        assert data is None


class TestAutoInspectInServerManager:
    """Tests for _enrich_param_error with usage example (MCPROXY-ibi)."""

    def test_includes_usage_example(self):
        """Enriched error message includes usage example."""
        error_msg = "Tool call failed: Unknown parameter 'quey'"
        result = _enrich_param_error("search", "test_server", error_msg, SAMPLE_TOOLS)
        assert "Usage:" in result
        assert "api.server('test_server').search" in result

    def test_usage_example_for_missing_param(self):
        """Usage example included for missing parameter errors."""
        error_msg = "Tool call failed: Missing required parameter 'path'"
        result = _enrich_param_error("read_file", "test_server", error_msg, SAMPLE_TOOLS)
        assert "Usage:" in result
        assert "api.server('test_server').read_file" in result

    def test_no_usage_for_non_param_error(self):
        """Non-param errors don't get usage appended."""
        error_msg = "Tool call failed: Internal server error"
        result = _enrich_param_error("search", "test_server", error_msg, SAMPLE_TOOLS)
        assert "Usage:" not in result

    def test_no_usage_for_missing_tool(self):
        """Missing tool doesn't cause errors in enrichment."""
        error_msg = "Tool call failed: Missing required parameter 'path'"
        result = _enrich_param_error(
            "nonexistent", "test_server", error_msg, SAMPLE_TOOLS
        )
        assert "Usage:" not in result


class TestSingleMatchAutoReturn:
    """Tests for search single-match auto-return (MCPROXY-ibi)."""

    def test_single_tool_match_adds_best_match(self):
        """Single tool match includes best_match with full schema."""
        from server.handlers.tools.search import _enrich_single_match
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "wikipedia": [
                    {
                        "name": "search",
                        "description": "Search Wikipedia",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "limit": {"type": "integer"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        }
        registry.get_tools.return_value = registry._manifest["tools_by_server"]["wikipedia"]

        results = {
            "matches": {"servers": [], "categories": [], "tools": ["wikipedia:search"]},
            "total_matches": 1,
        }
        _enrich_single_match(results, registry, None)

        assert "best_match" in results
        bm = results["best_match"]
        assert bm["server"] == "wikipedia"
        assert bm["name"] == "search"
        assert bm["description"] == "Search Wikipedia"
        assert "inputSchema" in bm
        assert "usage_example" in bm
        assert "api.server('wikipedia').search" in bm["usage_example"]

    def test_multiple_tool_matches_no_best_match(self):
        """Multiple tool matches do not add best_match."""
        from server.handlers.tools.search import _enrich_single_match
        from unittest.mock import MagicMock

        registry = MagicMock()

        results = {
            "matches": {
                "servers": [],
                "categories": [],
                "tools": ["wikipedia:search", "wikidata:search"],
            },
            "total_matches": 2,
        }
        _enrich_single_match(results, registry, None)

        assert "best_match" not in results

    def test_zero_tool_matches_no_best_match(self):
        """Zero tool matches do not add best_match."""
        from server.handlers.tools.search import _enrich_single_match

        results = {
            "matches": {"servers": [], "categories": [], "tools": []},
            "total_matches": 0,
        }
        _enrich_single_match(results, None, None)

        assert "best_match" not in results

    def test_no_registry_no_best_match(self):
        """Missing registry does not add best_match."""
        from server.handlers.tools.search import _enrich_single_match

        results = {
            "matches": {"servers": [], "categories": [], "tools": ["foo:bar"]},
            "total_matches": 1,
        }
        _enrich_single_match(results, None, None)

        assert "best_match" not in results

    def test_server_match_only_no_best_match(self):
        """Server name match (with no tool match) does not add best_match."""
        from server.handlers.tools.search import _enrich_single_match

        results = {
            "matches": {"servers": ["wikipedia"], "categories": [], "tools": []},
            "total_matches": 1,
        }
        _enrich_single_match(results, None, None)

        assert "best_match" not in results

    def test_best_match_tool_not_found_no_error(self):
        """If the tool isn't in the manifest, no best_match is added."""
        from server.handlers.tools.search import _enrich_single_match
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {"tools_by_server": {"wikipedia": []}}
        registry.get_tools.return_value = []

        results = {
            "matches": {"servers": [], "categories": [], "tools": ["wikipedia:search"]},
            "total_matches": 1,
        }
        _enrich_single_match(results, registry, None)

        assert "best_match" not in results

    def test_best_match_with_namespace_filter(self):
        """Namespace filter is passed to get_tools."""
        from server.handlers.tools.search import _enrich_single_match
        from unittest.mock import MagicMock

        registry = MagicMock()
        registry._manifest = {
            "tools_by_server": {
                "wikipedia": [
                    {
                        "name": "search",
                        "description": "Search Wikipedia",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        }
        registry.get_tools.return_value = registry._manifest["tools_by_server"]["wikipedia"]

        results = {
            "matches": {"servers": [], "categories": [], "tools": ["wikipedia:search"]},
            "total_matches": 1,
        }
        _enrich_single_match(results, registry, "docs")

        assert "best_match" in results
        registry.get_tools.assert_called_with("wikipedia", "docs")
