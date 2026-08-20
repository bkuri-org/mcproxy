import logging
import pytest
from unittest.mock import patch

from context_propagation import (
    process_args,
    sanitize_for_logs,
    ContextLogFilter,
    MAX_CONTEXT_SIZE,
)


@pytest.fixture
def base_args():
    return {"prompt": "do something", "model": "gpt-4"}


@pytest.fixture
def context_data():
    return {"user_id": "123", "session_id": "abc"}


@pytest.fixture
def large_context_data():
    return {"data": "x" * (MAX_CONTEXT_SIZE + 1)}


class TestDisabledStrictNoop:
    @patch("context_propagation.enabled", False)
    def test_noop_does_not_strip_aliases(self, base_args, context_data):
        base_args["context"] = "keep_me"
        base_args["Context"] = "keep_me_too"
        base_args["ctx"] = "keep_me_three"

        result = process_args("any_tool", base_args, context_data)

        assert result == base_args
        assert result["context"] == "keep_me"
        assert result["Context"] == "keep_me_too"
        assert result["ctx"] == "keep_me_three"

    @patch("context_propagation.enabled", False)
    def test_noop_does_not_inject_even_if_allowed(self, base_args, context_data):
        with patch("context_propagation.ALLOWED_TOOLS", {"any_tool"}):
            with patch("context_propagation.validate_schema", return_value=True):
                result = process_args("any_tool", base_args, context_data)

        assert "user_id" not in result
        assert "context" not in result

    @patch("context_propagation.enabled", False)
    def test_noop_prevents_alteration_of_existing_context_parameters(self, base_args, context_data):
        base_args["context"] = {"existing": "state"}
        original_context = base_args["context"]

        result = process_args("any_tool", base_args, context_data)

        assert result["context"] is original_context
        assert result["context"] == {"existing": "state"}


class TestEnabledAliasStripping:
    @patch("context_propagation.enabled", True)
    def test_strips_context_aliases_when_not_in_allowlist(self, base_args, context_data):
        base_args["context"] = "old"
        base_args["ctx"] = "old2"

        with patch("context_propagation.ALLOWED_TOOLS", set()):
            result = process_args("unallowed_tool", base_args, context_data)

        assert "context" not in result
        assert "ctx" not in result
        assert result["prompt"] == "do something"

    @patch("context_propagation.enabled", True)
    def test_strips_Context_capitalized_alias(self, base_args, context_data):
        base_args["Context"] = "old"

        with patch("context_propagation.ALLOWED_TOOLS", set()):
            result = process_args("unallowed_tool", base_args, context_data)

        assert "Context" not in result

    @patch("context_propagation.enabled", True)
    def test_strips_aliases_when_in_allowlist_but_schema_fails(self, base_args, context_data):
        base_args["ctx"] = "old"

        with patch("context_propagation.ALLOWED_TOOLS", {"allowed_tool"}):
            with patch("context_propagation.validate_schema", return_value=False):
                result = process_args("allowed_tool", base_args, context_data)

        assert "ctx" not in result
        assert "user_id" not in result


class TestInjectionGating:
    @patch("context_propagation.enabled", True)
    def test_injects_when_in_allowlist_and_schema_passes(self, base_args, context_data):
        with patch("context_propagation.ALLOWED_TOOLS", {"allowed_tool"}):
            with patch("context_propagation.validate_schema", return_value=True):
                result = process_args("allowed_tool", base_args, context_data)

        assert result.get("user_id") == "123"
        assert "ctx" not in result

    @patch("context_propagation.enabled", True)
    def test_no_injection_when_not_in_allowlist(self, base_args, context_data):
        with patch("context_propagation.ALLOWED_TOOLS", set()):
            result = process_args("other_tool", base_args, context_data)

        assert "user_id" not in result

    @patch("context_propagation.enabled", True)
    def test_no_injection_when_schema_fails(self, base_args, context_data):
        with patch("context_propagation.ALLOWED_TOOLS", {"allowed_tool"}):
            with patch("context_propagation.validate_schema", return_value=False):
                result = process_args("allowed_tool", base_args, context_data)

        assert "user_id" not in result


class TestSizeLimits:
    @patch("context_propagation.enabled", True)
    def test_injection_skipped_if_context_exceeds_limit(self, base_args, large_context_data):
        with patch("context_propagation.ALLOWED_TOOLS", {"allowed_tool"}):
            with patch("context_propagation.validate_schema", return_value=True):
                result = process_args("allowed_tool", base_args, large_context_data)

        assert "data" not in result
        assert "user_id" not in result

    @patch("context_propagation.enabled", True)
    def test_injection_succeeds_if_context_under_limit(self, base_args, context_data):
        with patch("context_propagation.ALLOWED_TOOLS", {"allowed_tool"}):
            with patch("context_propagation.validate_schema", return_value=True):
                result = process_args("allowed_tool", base_args, context_data)

        assert result.get("user_id") == "123"


class TestDeepSanitizerLogsOnly:
    def test_sanitizer_does_not_mutate_original_args(self):
        args = {"context": {"secret": "val"}, "prompt": "hello"}
        original_args = args.copy()
        # Deepcopy context to accurately test mutation
        import copy
        original_args["context"] = copy.deepcopy(args["context"])

        sanitized = sanitize_for_logs(args)

        assert args == original_args
        assert "secret" not in str(sanitized.get("context", {}))

    def test_sanitizer_strips_deep_nested_secrets_for_logs(self):
        args = {
            "context": {
                "history": [{"role": "user", "secret_token": "abc123"}]
            }
        }
        sanitized = sanitize_for_logs(args)
        assert "abc123" not in str(sanitized)
        # Ensure structure is maintained for logs but value is masked
        assert "[REDACTED]" in str(sanitized["context"]["history"][0]["secret_token"])


class TestLogFilterDefenseInDepth:
    def test_log_filter_strips_context_from_log_records(self):
        log_filter = ContextLogFilter()
        payload = {"context": {"secret": "s"}, "prompt": "hi"}
        record = logging.LogRecord(
            "test", logging.INFO, "path", 1, "Message: %s", (payload,), None
        )
        log_filter.filter(record)

        assert "secret" not in record.getMessage()
        # Ensure original payload passed to log is also mutated as defense-in-depth
        assert "secret" not in str(payload)

    def test_log_filter_handles_missing_context_gracefully(self):
        log_filter = ContextLogFilter()
        payload = {"prompt": "hi"}
        record = logging.LogRecord(
            "test", logging.INFO, "path", 1, "Message: %s", (payload,), None
        )
        assert log_filter.filter(record) is True
        assert "hi" in record.getMessage()
