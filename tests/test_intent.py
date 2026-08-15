import json
import pytest
from unittest.mock import MagicMock, patch
from reasoning.intent import Intent, classify_intent, normalize_intent, validate_intent, INTENT_PROMPT


# ---------------------------------------------------------------------------
# Intent schema
# ---------------------------------------------------------------------------

class TestIntent:
    def test_create_with_all_fields(self):
        intent = Intent(action="search", target="files", modifiers=["recent", "large"])
        assert intent.action == "search"
        assert intent.target == "files"
        assert intent.modifiers == ["recent", "large"]

    def test_create_without_modifiers_defaults_to_empty_list(self):
        intent = Intent(action="delete", target="temp")
        assert intent.modifiers == []

    def test_create_explicit_none_modifiers_normalizes_to_empty_list(self):
        intent = Intent(action="list", target="tables", modifiers=None)
        assert intent.modifiers == []

    def test_equality(self):
        a = Intent(action="a", target="b", modifiers=["c"])
        b = Intent(action="a", target="b", modifiers=["c"])
        assert a == b

    def test_inequality(self):
        a = Intent(action="a", target="b")
        b = Intent(action="x", target="b")
        assert a != b

    def test_modifiers_are_isolated(self):
        intent = Intent(action="a", target="b", modifiers=["x"])
        intent.modifiers.append("y")
        intent2 = Intent(action="a", target="b", modifiers=["x"])
        assert intent2.modifiers == ["x"]

    def test_to_dict_round_trip(self):
        original = Intent(action="move", target="folder", modifiers=["recursive"])
        d = original.to_dict()
        restored = Intent.from_dict(d)
        assert restored == original

    def test_from_dict_missing_modifiers(self):
        d = {"action": "copy", "target": "file.txt"}
        intent = Intent.from_dict(d)
        assert intent.modifiers == []

    def test_from_dict_extra_keys_ignored(self):
        d = {"action": "read", "target": "log", "modifiers": [], "extra": True}
        intent = Intent.from_dict(d)
        assert intent.action == "read"

    def test_repr_contains_action_and_target(self):
        intent = Intent(action="summarize", target="report")
        r = repr(intent)
        assert "summarize" in r
        assert "report" in r


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateIntent:
    def test_valid_intent_passes(self):
        intent = Intent(action="search", target="emails")
        assert validate_intent(intent) is True

    def test_missing_action_raises(self):
        intent = Intent(action="", target="x")
        with pytest.raises(ValueError, match="action"):
            validate_intent(intent)

    def test_missing_target_raises(self):
        intent = Intent(action="x", target="")
        with pytest.raises(ValueError, match="target"):
            validate_intent(intent)

    def test_none_intent_raises(self):
        with pytest.raises(ValueError):
            validate_intent(None)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalizeIntent:
    def test_none_modifiers_becomes_empty_list(self):
        intent = Intent(action="a", target="b", modifiers=None)
        normalized = normalize_intent(intent)
        assert normalized.modifiers == []
        assert isinstance(normalized.modifiers, list)

    def test_string_modifiers_coerced_to_list(self):
        intent = Intent(action="a", target="b", modifiers="fast")
        normalized = normalize_intent(intent)
        assert normalized.modifiers == ["fast"]

    def test_already_list_unchanged(self):
        intent = Intent(action="a", target="b", modifiers=["x"])
        assert normalize_intent(intent).modifiers == ["x"]

    def test_whitespace_stripped_from_action_target(self):
        intent = Intent(action="  search  ", target="  files  ")
        n = normalize_intent(intent)
        assert n.action == "search"
        assert n.target == "files"

    def test_lowercase_action_and_target(self):
        intent = Intent(action="SEARCH", target="FILES")
        n = normalize_intent(intent)
        assert n.action == "search"
        assert n.target == "files"

    def test_modifiers_lowercased_and_stripped(self):
        intent = Intent(action="a", target="b", modifiers=["  RECENT  ", "LARGE"])
        n = normalize_intent(intent)
        assert n.modifiers == ["recent", "large"]

    def test_empty_modifier_strings_removed(self):
        intent = Intent(action="a", target="b", modifiers=["", "  ", "valid"])
        n = normalize_intent(intent)
        assert n.modifiers == ["valid"]

    def test_duplicate_modifiers_deduplicated(self):
        intent = Intent(action="a", target="b", modifiers=["fast", "fast", "slow"])
        n = normalize_intent(intent)
        assert n.modifiers == ["fast", "slow"]


# ---------------------------------------------------------------------------
# Classification (llm_call injection)
# ---------------------------------------------------------------------------

class TestClassifyIntent:
    def _make_llm_return(self, obj: dict) -> MagicMock:
        mock = MagicMock(return_value=json.dumps(obj))
        return mock

    def test_basic_classification(self):
        llm = self._make_llm_return({"action": "search", "target": "emails"})
        result = classify_intent("find my recent emails", llm_call=llm)
        assert result.action == "search"
        assert result.target == "emails"
        assert result.modifiers == []

    def test_classification_with_modifiers(self):
        llm = self._make_llm_return({
            "action": "delete",
            "target": "temp_files",
            "modifiers": ["older_than_30d", "not_starred"],
        })
        result = classify_intent("remove old temp files that aren't starred", llm_call=llm)
        assert result.modifiers == ["older_than_30d", "not_starred"]

    def test_missing_modifiers_in_llm_response_defaults(self):
        llm = self._make_llm_return({"action": "list", "target": "tables"})
        result = classify_intent("show tables", llm_call=llm)
        assert result.modifiers == []

    def test_normalization_applied_after_parsing(self):
        llm = self._make_llm_return({"action": "  SEARCH  ", "target": " FILES ", "modifiers": [" RECENT "]})
        result = classify_intent("recent files", llm_call=llm)
        assert result.action == "search"
        assert result.target == "files"
        assert result.modifiers == ["recent"]

    def test_invalid_json_from_llm_raises(self):
        llm = MagicMock(return_value="not json at all")
        with pytest.raises(ValueError, match="JSON"):
            classify_intent("do something", llm_call=llm)

    def test_missing_required_key_raises(self):
        llm = self._make_llm_return({"target": "x"})
        with pytest.raises(ValueError, match="action"):
            classify_intent("do something", llm_call=llm)

    def test_llm_called_with_prompt_and_text(self):
        llm = self._make_llm_return({"action": "summarize", "target": "document"})
        classify_intent("summarize this document", llm_call=llm)
        llm.assert_called_once()
        call_arg = llm.call_args[0][0]
        assert "summarize this document" in call_arg

    def test_validation_applied_after_parsing(self):
        llm = self._make_llm_return({"action": "", "target": "x"})
        with pytest.raises(ValueError, match="action"):
            classify_intent("do something", llm_call=llm)

    def test_empty_modifiers_array_handled(self):
        llm = self._make_llm_return({"action": "read", "target": "log", "modifiers": []})
        result = classify_intent("read the log", llm_call=llm)
        assert result.modifiers == []

    def test_extra_keys_in_llm_response_ignored(self):
        llm = self._make_llm_return({"action": "read", "target": "file", "confidence": 0.9})
        result = classify_intent("read file", llm_call=llm)
        assert result.action == "read"


# ---------------------------------------------------------------------------
# Prompt constant
# ---------------------------------------------------------------------------

class TestIntentPrompt:
    def test_prompt_contains_required_terms(self):
        assert "action" in INTENT_PROMPT
        assert "target" in INTENT_PROMPT
        assert "modifiers" in INTENT_PROMPT
        assert "JSON" in INTENT_PROMPT

    def test_prompt_is_string(self):
        assert isinstance(INTENT_PROMPT, str)

    def test_prompt_is_non_empty(self):
        assert len(INTENT_PROMPT.strip()) > 0


# ---------------------------------------------------------------------------
# Integration-style: from_dict + validate + normalize pipeline
# ---------------------------------------------------------------------------

class TestPipeline:
    def test_full_pipeline(self):
        raw = {"action": "  LIST  ", "target": "  users  ", "modifiers": None}
        intent = Intent.from_dict(raw)
        validate_intent(intent)
        final = normalize_intent(intent)
        assert final == Intent(action="list", target="users", modifiers=[])
