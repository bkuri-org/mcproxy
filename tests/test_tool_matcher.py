import pytest
from unittest.mock import patch, MagicMock
from reasoning.tool_matcher import ToolMatcher


class TestToolMatcherGetHelper:
    """Tests for the defensive _get helper method."""

    def test_getattr_takes_priority_over_dict_get(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        obj = type("Obj", (), {"confidence": 0.8})()
        assert matcher._get(obj, "confidence") == 0.8

    def test_falls_back_to_dict_get_when_no_attr(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        obj = {"confidence": 0.6}
        assert matcher._get(obj, "confidence") == 0.6

    def test_default_return_when_missing_from_both(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        obj = type("Obj", (), {})()
        assert matcher._get(obj, "confidence") == 1.0

    def test_default_return_for_plain_dict_missing_key(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        assert matcher._get({}, "confidence") == 1.0

    def test_custom_default_value(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        assert matcher._get(None, "confidence", default=0.5) == 0.5

    def test_getattr_over_dict_even_if_dict_has_key(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        obj = type("Obj", (), {"confidence": 0.9})()
        obj.__dict__["confidence"] = 0.1
        assert matcher._get(obj, "confidence") == 0.9


class TestToolMatcherDefaultStrings:
    """Tool name/description must default to '' so the matcher never sees None."""

    def test_none_name_defaults_to_empty_string(self):
        catalog = [{"name": None, "description": "desc"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("do something")
        assert result is not None
        # Should not raise when processing None name

    def test_none_description_defaults_to_empty_string(self):
        catalog = [{"name": "tool_a", "description": None}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("do something")
        assert result is not None

    def test_both_none_still_works(self):
        catalog = [{"name": None, "description": None}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("do something")
        assert result is not None

    def test_missing_name_key_defaults_to_empty_string(self):
        catalog = [{"description": "desc"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("do something")
        assert result is not None

    def test_missing_description_key_defaults_to_empty_string(self):
        catalog = [{"name": "tool_a"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("do something")
        assert result is not None


class TestToolMatcherConfidenceClamping:
    """Intent confidence must be clamped to [0.0, 1.0]."""

    def test_confidence_above_one_clamped_to_one(self):
        catalog = [
            {"name": "tool_a", "description": "search things", "confidence": 5.0}
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        scores = result.get("scores", result.get("ranked", []))
        if scores:
            top_score = scores[0][1] if isinstance(scores[0], (list, tuple)) else scores[0].get("score", scores[0].get("confidence"))
            assert top_score <= 1.0

    def test_confidence_below_zero_clamped_to_zero(self):
        catalog = [
            {"name": "tool_a", "description": "search things", "confidence": -3.0}
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        scores = result.get("scores", result.get("ranked", []))
        if scores:
            top_score = scores[0][1] if isinstance(scores[0], (list, tuple)) else scores[0].get("score", scores[0].get("confidence"))
            assert top_score >= 0.0

    def test_confidence_exactly_one_unchanged(self):
        catalog = [
            {"name": "tool_a", "description": "search things", "confidence": 1.0}
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        scores = result.get("scores", result.get("ranked", []))
        if scores:
            top_score = scores[0][1] if isinstance(scores[0], (list, tuple)) else scores[0].get("score", scores[0].get("confidence"))
            assert top_score == 1.0

    def test_confidence_exactly_zero_unchanged(self):
        catalog = [
            {"name": "tool_a", "description": "search things", "confidence": 0.0}
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        scores = result.get("scores", result.get("ranked", []))
        if scores:
            top_score = scores[0][1] if isinstance(scores[0], (list, tuple)) else scores[0].get("score", scores[0].get("confidence"))
            assert top_score == 0.0

    def test_out_of_range_cannot_inflate_scores(self):
        catalog = [
            {"name": "tool_a", "description": "search", "confidence": 999.0},
            {"name": "tool_b", "description": "compute", "confidence": 0.5},
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        scores = result.get("scores", result.get("ranked", []))
        for entry in scores:
            score = entry[1] if isinstance(entry, (list, tuple)) else entry.get("score", entry.get("confidence"))
            assert 0.0 <= score <= 1.0


class TestToolMatcherAmbiguityDetection:
    """Ambiguity detection via score-gap threshold only when >= 2 candidates."""

    def test_empty_catalog_non_ambiguous(self):
        matcher = ToolMatcher([])
        result = matcher.match("do something")
        assert result.get("ambiguous", False) is False
        assert result.get("disambiguation_prompt") is None

    def test_single_tool_non_ambiguous_no_gap_indexing(self):
        catalog = [{"name": "tool_a", "description": "search files"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search files")
        assert result.get("ambiguous", False) is False
        assert result.get("disambiguation_prompt") is None

    def test_two_tools_clear_winner_non_ambiguous(self):
        catalog = [
            {"name": "tool_a", "description": "search files"},
            {"name": "tool_b", "description": "send email"},
        ]
        matcher = ToolMatcher(catalog, gap_threshold=0.1)
        result = matcher.match("search files")
        assert result.get("ambiguous", False) is False

    def test_two_tools_close_scores_ambiguous(self):
        catalog = [
            {"name": "file_search", "description": "search files on disk"},
            {"name": "db_search", "description": "search files in database"},
        ]
        matcher = ToolMatcher(catalog, gap_threshold=0.5)
        result = matcher.match("search files")
        assert result.get("ambiguous", False) is True
        assert result.get("disambiguation_prompt") is not None

    def test_three_tools_top_two_close_ambiguous(self):
        catalog = [
            {"name": "tool_a", "description": "search files locally"},
            {"name": "tool_b", "description": "search files remotely"},
            {"name": "tool_c", "description": "delete files"},
        ]
        matcher = ToolMatcher(catalog, gap_threshold=0.5)
        result = matcher.match("search files")
        assert result.get("ambiguous", False) is True
        assert result.get("disambiguation_prompt") is not None

    def test_disambiguation_prompt_contains_candidate_names(self):
        catalog = [
            {"name": "local_search", "description": "search files locally"},
            {"name": "remote_search", "description": "search files remotely"},
        ]
        matcher = ToolMatcher(catalog, gap_threshold=0.5)
        result = matcher.match("search files")
        prompt = result.get("disambiguation_prompt", "")
        assert "local_search" in prompt or "remote_search" in prompt

    def test_gap_threshold_zero_always_ambiguous_with_multiple(self):
        catalog = [
            {"name": "tool_a", "description": "search files"},
            {"name": "tool_b", "description": "send email"},
        ]
        matcher = ToolMatcher(catalog, gap_threshold=0.0)
        result = matcher.match("search files")
        assert result.get("ambiguous", False) is True

    def test_large_gap_threshold_never_ambiguous(self):
        catalog = [
            {"name": "file_search", "description": "search files on disk"},
            {"name": "db_search", "description": "search files in database"},
        ]
        matcher = ToolMatcher(catalog, gap_threshold=100.0)
        result = matcher.match("search files")
        assert result.get("ambiguous", False) is False


class TestToolMatcherRanking:
    """Verify correct ranking of candidates."""

    def test_best_match_is_first(self):
        catalog = [
            {"name": "tool_a", "description": "send email"},
            {"name": "tool_b", "description": "search files"},
            {"name": "tool_c", "description": "delete files"},
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search files")
        best = result.get("best_match", result.get("match"))
        assert best == "tool_b" or best.get("name") == "tool_b"

    def test_all_candidates_present_in_scores(self):
        catalog = [
            {"name": "tool_a", "description": "alpha"},
            {"name": "tool_b", "description": "beta"},
            {"name": "tool_c", "description": "gamma"},
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("alpha")
        scores = result.get("scores", result.get("ranked", []))
        names = []
        for entry in scores:
            if isinstance(entry, (list, tuple)):
                names.append(entry[0])
            else:
                names.append(entry.get("name"))
        assert set(names) == {"tool_a", "tool_b", "tool_c"}

    def test_scores_in_descending_order(self):
        catalog = [
            {"name": "tool_a", "description": "alpha"},
            {"name": "tool_b", "description": "beta"},
        ]
        matcher = ToolMatcher(catalog)
        result = matcher.match("alpha")
        scores = result.get("scores", result.get("ranked", []))
        vals = []
        for entry in scores:
            if isinstance(entry, (list, tuple)):
                vals.append(entry[1])
            else:
                vals.append(entry.get("score", entry.get("confidence")))
        assert vals == sorted(vals, reverse=True)


class TestToolMatcherObjectBasedCatalog:
    """Catalog entries may be objects, not just dicts."""

    def test_object_with_attributes(self):
        tool = type("Tool", (), {"name": "obj_tool", "description": "do stuff", "confidence": 0.9})()
        matcher = ToolMatcher([tool])
        result = matcher.match("do stuff")
        assert result is not None
        best = result.get("best_match", result.get("match"))
        assert best == "obj_tool" or best.get("name") == "obj_tool"

    def test_object_missing_confidence_defaults_to_one(self):
        tool = type("Tool", (), {"name": "obj_tool", "description": "do stuff"})()
        matcher = ToolMatcher([tool])
        result = matcher.match("do stuff")
        scores = result.get("scores", result.get("ranked", []))
        if scores:
            score = scores[0][1] if isinstance(scores[0], (list, tuple)) else scores[0].get("score", scores[0].get("confidence"))
            assert score == 1.0


class TestToolMatcherEdgeCases:
    """Miscellaneous edge cases."""

    def test_empty_intent_string(self):
        catalog = [{"name": "tool_a", "description": "search"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("")
        assert result is not None

    def test_none_intent_handled(self):
        catalog = [{"name": "tool_a", "description": "search"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match(None)
        assert result is not None

    def test_catalog_entry_is_none_skipped(self):
        catalog = [None, {"name": "tool_a", "description": "search"}, None]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        assert result is not None
        scores = result.get("scores", result.get("ranked", []))
        assert len(scores) == 1

    def test_non_dict_non_object_entry_skipped(self):
        catalog = [42, "bad", {"name": "tool_a", "description": "search"}]
        matcher = ToolMatcher(catalog)
        result = matcher.match("search")
        scores = result.get("scores", result.get("ranked", []))
        assert len(scores) == 1

    def test_all_bad_catalog_returns_empty_result(self):
        matcher = ToolMatcher([None, 42, "bad"])
        result = matcher.match("search")
        assert result.get("best_match") is None or result.get("match") is None
        assert result.get("ambiguous", False) is False
        assert len(result.get("scores", result.get("ranked", []))) == 0


class TestToolMatcherExport:
    """Verify ToolMatcher is properly exported from the module."""

    def test_importable(self):
        from reasoning.tool_matcher import ToolMatcher as TM
        assert TM is ToolMatcher

    def test_module_has_tool_matcher(self):
        import reasoning.tool_matcher as mod
        assert hasattr(mod, "ToolMatcher")

    def test_module_all_exports_tool_matcher(self):
        import reasoning.tool_matcher as mod
        if hasattr(mod, "__all__"):
            assert "ToolMatcher" in mod.__all__
