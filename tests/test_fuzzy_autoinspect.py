import pytest
from unittest.mock import patch, MagicMock, call
import logging
import json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name="do_thing", params=None):
    """Return a minimal tool dict as the registry would expose it."""
    return {
        "name": name,
        "description": f"Does {name}",
        "parameters": params or {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "The target resource"},
                "mode": {"type": "string", "enum": ["fast", "slow"]},
            },
            "required": ["target"],
        },
    }


def _make_registry(tools=None):
    reg = MagicMock()
    reg.list_tools.return_value = tools or [_make_tool()]
    return reg


class FakeSanitizer:
    """Stand-in for the real error-formatting sanitizer."""
    def __init__(self, strip_keys=None):
        self._strip = set(strip_keys or ["example", "internal"])

    def sanitize(self, payload):
        if isinstance(payload, dict):
            return {k: v for k, v in payload.items() if k not in self._strip}
        if isinstance(payload, str):
            return payload
        return payload


# ---------------------------------------------------------------------------
# Tests: fuzzy=True is per-request, search-only, no global default
# ---------------------------------------------------------------------------

class TestFuzzySearchOptIn:
    """fuzzy=True must be passed per-request; it only affects search."""

    def test_search_without_fuzzy_is_exact(self):
        from mcp_server.search import search_tools
        reg = _make_registry([_make_tool("do_thing"), _make_tool("do_other")])
        with patch("mcp_server.search.get_registry", return_value=reg):
            results = search_tools("do_thnig", fuzzy=False)
        assert len(results) == 0  # exact match misses the typo

    def test_search_with_fuzzy_finds_typo(self):
        from mcp_server.search import search_tools
        reg = _make_registry([_make_tool("do_thing"), _make_tool("do_other")])
        with patch("mcp_server.search.get_registry", return_value=reg):
            results = search_tools("do_thnig", fuzzy=True)
        names = [r["name"] for r in results]
        assert "do_thing" in names

    def test_search_fuzzy_defaults_to_false(self):
        """Omitting fuzzy must NOT enable fuzzy matching (no global default)."""
        from mcp_server.search import search_tools
        reg = _make_registry([_make_tool("do_thing")])
        with patch("mcp_server.search.get_registry", return_value=reg):
            results = search_tools("do_thnig")  # no fuzzy kwarg
        assert len(results) == 0

    def test_fuzzy_kwarg_ignored_by_execute(self):
        """Execute always uses exact-match regardless of fuzzy=True."""
        from mcp_server.execute import execute_tool
        reg = _make_registry([_make_tool("do_thing")])
        with patch("mcp_server.execute.get_registry", return_value=reg):
            with pytest.raises(Exception, match="not found|unknown"):
                execute_tool("do_thnig", {"target": "x"}, fuzzy=True)


# ---------------------------------------------------------------------------
# Tests: inline schema on single fuzzy match, gated by inspect scope
# ---------------------------------------------------------------------------

class TestInlineSchemaOnFuzzyMatch:
    """When fuzzy search yields exactly one result AND the caller has the
    'inspect' scope, the response includes the tool's parameter schema inline.
    Without inspect scope, fall back to a describe-pointer URL."""

    def test_single_fuzzy_match_includes_schema_with_inspect_scope(self):
        from mcp_server.search import search_tools
        reg = _make_registry([_make_tool("do_thing")])
        with patch("mcp_server.search.get_registry", return_value=reg):
            results = search_tools("do_thnig", fuzzy=True, scopes=["inspect"])
        assert len(results) == 1
        assert "parameters" in results[0]
        assert results[0]["parameters"]["type"] == "object"

    def test_single_fuzzy_match_describe_pointer_without_inspect_scope(self):
        from mcp_server.search import search_tools
        reg = _make_registry([_make_tool("do_thing")])
        with patch("mcp_server.search.get_registry", return_value=reg):
            results = search_tools("do_thnig", fuzzy=True, scopes=[])
        assert len(results) == 1
        assert "parameters" not in results[0]
        assert "describe_pointer" in results[0]

    def test_multiple_fuzzy_matches_omit_inline_schema(self):
        """Inline schema is only for single-match disambiguation."""
        from mcp_server.search import search_tools
        reg = _make_registry([
            _make_tool("do_thing_a"),
            _make_tool("do_thing_b"),
        ])
        with patch("mcp_server.search.get_registry", return_value=reg):
            results = search_tools("do_thnig", fuzzy=True, scopes=["inspect"])
        for r in results:
            assert "parameters" not in r


# ---------------------------------------------------------------------------
# Tests: enriched execute parameter errors
# ---------------------------------------------------------------------------

class TestEnrichedExecuteErrors:
    """When execute receives bad parameters the error should contain:
    - schema  (the tool's JSON Schema)
    - hints   (human-readable fix suggestions)
    - example (a valid example payload, via refactored inspect/example_gen)
    All of this must pass through the error-formatting sanitizer before
    being returned to the caller."""

    def test_missing_required_enriched(self):
        from mcp_server.execute import execute_tool
        from mcp_server.errors import format_error
        reg = _make_registry([_make_tool("do_thing")])
        sanitizer = FakeSanitizer()

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.example_gen", return_value={"target": "example.com", "mode": "fast"}):
            with pytest.raises(Exception) as exc_info:
                execute_tool("do_thing", {}, scopes=["inspect"])

        error_payload = json.loads(str(exc_info.value))
        # After sanitization 'example' key is stripped
        assert "schema" in error_payload
        assert "hints" in error_payload
        assert "example" not in error_payload  # sanitized away

    def test_invalid_enum_enriched(self):
        from mcp_server.execute import execute_tool
        reg = _make_registry([_make_tool("do_thing")])
        sanitizer = FakeSanitizer()

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.example_gen", return_value={"target": "x", "mode": "fast"}):
            with pytest.raises(Exception) as exc_info:
                execute_tool("do_thing", {"target": "x", "mode": "invalid"}, scopes=["inspect"])

        error_payload = json.loads(str(exc_info.value))
        assert any("fast" in h or "slow" in h for h in error_payload.get("hints", []))

    def test_extra_property_enriched(self):
        from mcp_server.execute import execute_tool
        reg = _make_registry([_make_tool("do_thing")])
        sanitizer = FakeSanitizer()

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.example_gen", return_value={"target": "x"}):
            with pytest.raises(Exception) as exc_info:
                execute_tool("do_thing", {"target": "x", "bogus": True}, scopes=["inspect"])

        error_payload = json.loads(str(exc_info.value))
        assert any("bogus" in h for h in error_payload.get("hints", []))

    def test_enrichment_omitted_without_inspect_scope(self):
        """Without inspect scope the error is plain — no schema/hints/example."""
        from mcp_server.execute import execute_tool
        reg = _make_registry([_make_tool("do_thing")])
        sanitizer = FakeSanitizer()

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer):
            with pytest.raises(Exception) as exc_info:
                execute_tool("do_thing", {}, scopes=[])

        error_payload = json.loads(str(exc_info.value))
        assert "schema" not in error_payload
        assert "hints" not in error_payload


# ---------------------------------------------------------------------------
# Tests: sanitized content is returned to caller, never logged verbatim
# ---------------------------------------------------------------------------

class TestSanitizationAndLogging:
    """Enriched error payloads must go through the sanitizer before being
    returned, and the logger must never receive the raw (unsanitized) payload."""

    def test_logger_receives_sanitized_only(self):
        from mcp_server.execute import execute_tool
        reg = _make_registry([_make_tool("do_thing")])
        sanitizer = FakeSanitizer(strip_keys=["example", "schema"])
        mock_logger = MagicMock(spec=logging.Logger)

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.get_logger", return_value=mock_logger), \
             patch("mcp_server.execute.example_gen", return_value={"target": "x"}):
            with pytest.raises(Exception):
                execute_tool("do_thing", {}, scopes=["inspect"])

        # Find the log call that recorded the error payload
        logged_args = []
        for _name, args, kwargs in mock_logger.method_calls:
            logged_args.extend(args)
            logged_args.extend(kwargs.values())

        for arg in logged_args:
            if isinstance(arg, dict):
                assert "example" not in arg
                assert "schema" not in arg

    def test_caller_gets_sanitized_payload(self):
        from mcp_server.execute import execute_tool
        reg = _make_registry([_make_tool("do_thing")])
        sanitizer = FakeSanitizer(strip_keys=["example"])

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.example_gen", return_value={"target": "example.com"}):
            with pytest.raises(Exception) as exc_info:
                execute_tool("do_thing", {}, scopes=["inspect"])

        payload = json.loads(str(exc_info.value))
        assert "schema" in payload
        assert "hints" in payload
        assert "example" not in payload  # stripped by sanitizer


# ---------------------------------------------------------------------------
# Tests: refactored inspect / example_gen integration
# ---------------------------------------------------------------------------

class TestInspectExampleGenIntegration:
    """example_gen is the single source of truth for example payloads.
    Execute errors delegate to it rather than duplicating logic."""

    def test_example_gen_called_with_correct_schema(self):
        from mcp_server.execute import execute_tool
        tool = _make_tool("do_thing")
        reg = _make_registry([tool])
        sanitizer = FakeSanitizer()

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.example_gen", return_value={"target": "x"}) as mock_eg:
            with pytest.raises(Exception):
                execute_tool("do_thing", {}, scopes=["inspect"])

            mock_eg.assert_called_once_with(tool["parameters"])

    def test_example_gen_graceful_on_complex_schema(self):
        """If example_gen cannot produce an example it must return None,
        and the error enrichment must still work (just without example)."""
        from mcp_server.execute import execute_tool
        tool = _make_tool("do_thing", params={"type": "object", "properties": {"x": {"type": "string"}}})
        reg = _make_registry([tool])
        sanitizer = FakeSanitizer()

        with patch("mcp_server.execute.get_registry", return_value=reg), \
             patch("mcp_server.execute.get_sanitizer", return_value=sanitizer), \
             patch("mcp_server.execute.example_gen", return_value=None):
            with pytest.raises(Exception) as exc_info:
                execute_tool("do_thing", {}, scopes=["inspect"])

            payload = json.loads(str(exc_info.value))
            assert "schema" in payload
            assert "hints" in payload
            # example key may be absent entirely when generator returns None
            assert payload.get("example") is None or "example" not in payload
