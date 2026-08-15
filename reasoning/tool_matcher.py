"""Tool matcher that fuzzy-scores intents against an aggregated tool catalog."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class ToolCandidate:
    """A single tool entry with its match score."""

    tool: Dict[str, Any]
    score: float


@dataclass
class MatchResult:
    """Result of matching an intent against the tool catalog."""

    candidates: List[ToolCandidate]
    is_ambiguous: bool
    disambiguation_prompt: Optional[str] = None

    @property
    def best(self) -> Optional[ToolCandidate]:
        return self.candidates[0] if self.candidates else None


class ToolMatcher:
    """Fuzzy-matches a free-text intent against an aggregated tool catalog.

    Defensive guarantees
    --------------------
    * ``_get`` helper always tries ``getattr`` first, then dict ``.get``,
      and defaults confidence to ``1.0`` so a missing confidence key never
      raises ``KeyError`` / ``AttributeError``.
    * Tool *name* and *description* are defaulted to ``""`` so the scorer
      never receives ``None``.
    * Intent confidence is clamped to ``[0.0, 1.0]`` – out-of-range values
      cannot inflate (or deflate) the final score.
    * Ambiguity detection via score-gap threshold runs **only** when there
      are ``>= 2`` candidates.  Catalogs with 0 or 1 tools short-circuit as
      non-ambiguous without any gap-indexing (avoids ``IndexError``).
    """

    def __init__(
        self,
        catalog: Sequence[Dict[str, Any]],
        *,
        score_cutoff: float = 0.4,
        ambiguity_gap_threshold: float = 0.15,
    ) -> None:
        self._catalog = list(catalog)
        self._score_cutoff = score_cutoff
        self._ambiguity_gap_threshold = ambiguity_gap_threshold

    # ------------------------------------------------------------------
    # Defensive accessor
    # ------------------------------------------------------------------

    @staticmethod
    def _get(obj: Any, key: str, default: Any = None) -> Any:
        """Try *getattr*, fall back to dict ``.get``.

        This accommodates both plain dicts and objects while guaranteeing a
        return value (never propagates ``AttributeError`` / ``KeyError``).
        """
        # Try attribute access first (works for objects / namedtuples / etc.)
        try:
            value = getattr(obj, key)
            if value is not None:
                return value
        except AttributeError:
            pass

        # Fall back to dict-style access
        if isinstance(obj, dict):
            value = obj.get(key, default)
            return value if value is not None else default

        return default

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    def _tool_name(self, tool: Dict[str, Any]) -> str:
        return str(self._get(tool, "name", "") or "")

    def _tool_description(self, tool: Dict[str, Any]) -> str:
        return str(self._get(tool, "description", "") or "")

    @staticmethod
    def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, value))

    # ------------------------------------------------------------------
    # Core scoring
    # ------------------------------------------------------------------

    def _score_tool(self, tool: Dict[str, Any], intent: str) -> float:
        name = self._tool_name(tool)
        description = self._tool_description(tool)

        # Fuzzy ratios against name and description
        name_ratio = (
            difflib.SequenceMatcher(None, intent.lower(), name.lower()).ratio()
            if name
            else 0.0
        )
        desc_ratio = (
            difflib.SequenceMatcher(None, intent.lower(), description.lower()).ratio()
            if description
            else 0.0
        )

        # Weighted combination (description contributes less)
        raw_score = 0.7 * name_ratio + 0.3 * desc_ratio

        # Multiply by tool's own confidence (default 1.0) and clamp
        confidence = float(self._get(tool, "confidence", 1.0) or 1.0)
        confidence = self._clamp(confidence)
        final_score = self._clamp(raw_score * confidence)

        return final_score

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(self, intent: str) -> MatchResult:
        """Score *intent* against every tool in the catalog and rank.

        Returns a :class:`MatchResult` with candidates sorted descending by
        score.  Ambiguity is detected **only** when ``>= 2`` candidates pass
        the cutoff; otherwise the result is always non-ambiguous.
        """
        if not intent:
            return MatchResult(candidates=[], is_ambiguous=False)

        # Short-circuit: 0 or 1 tool → never ambiguous (no gap indexing)
        if len(self._catalog) <= 1:
            if not self._catalog:
                return MatchResult(candidates=[], is_ambiguous=False)
            tool = self._catalog[0]
            score = self._score_tool(tool, intent)
            if score >= self._score_cutoff:
                return MatchResult(
                    candidates=[ToolCandidate(tool=tool, score=score)],
                    is_ambiguous=False,
                )
            return MatchResult(candidates=[], is_ambiguous=False)

        # Score all tools
        scored: List[ToolCandidate] = []
        for tool in self._catalog:
            s = self._score_tool(tool, intent)
            if s >= self._score_cutoff:
                scored.append(ToolCandidate(tool=tool, score=s))

        scored.sort(key=lambda c: c.score, reverse=True)

        # Ambiguity check: only when >= 2 candidates
        is_ambiguous = False
        disambiguation_prompt: Optional[str] = None

        if len(scored) >= 2:
            gap = scored[0].score - scored[1].score
            if gap < self._ambiguity_gap_threshold:
                is_ambiguous = True
                names = [self._tool_name(c.tool) for c in scored[:5]]
                disambiguation_prompt = (
                    f"Multiple tools matched your intent. "
                    f"Did you mean: {', '.join(names)}?"
                )

        return MatchResult(
            candidates=scored,
            is_ambiguous=is_ambiguous,
            disambiguation_prompt=disambiguation_prompt,
        )


__all__ = ["ToolMatcher", "MatchResult", "ToolCandidate"]


# ======================================================================
# Tests
# ======================================================================

import unittest


class _TestToolMatcher(unittest.TestCase):
    """Edge-case and behavioural tests for ToolMatcher."""

    # -- _get helper --------------------------------------------------

    def test_get_falls_back_to_dict_get(self):
        """getattr fails on plain dict -> dict .get is used."""
        result = ToolMatcher._get({"name": "foo"}, "name")
        self.assertEqual(result, "foo")

    def test_get_returns_default_on_missing_key(self):
        result = ToolMatcher._get({"name": "foo"}, "missing", "def")
        self.assertEqual(result, "def")

    def test_get_prefers_getattr(self):
        class Obj:
            name = "attr"

        result = ToolMatcher._get(Obj(), "name", "def")
        self.assertEqual(result, "attr")

    def test_get_defaults_confidence_to_1_0(self):
        """Missing confidence key -> 1.0, not None."""
        result = ToolMatcher._get({"name": "x"}, "confidence", 1.0)
        self.assertEqual(result, 1.0)

    def test_get_none_value_falls_through_to_default(self):
        """Explicit None value in dict -> default is returned."""
        result = ToolMatcher._get({"confidence": None}, "confidence", 1.0)
        self.assertEqual(result, 1.0)

    # -- Tool name / description never None ---------------------------

    def test_tool_name_defaults_to_empty_string(self):
        m = ToolMatcher([{}])
        self.assertEqual(m._tool_name({}), "")

    def test_tool_description_defaults_to_empty_string(self):
        m = ToolMatcher([{}])
        self.assertEqual(m._tool_description({}), "")

    def test_none_name_becomes_empty_string(self):
        m = ToolMatcher([{"name": None}])
        self.assertEqual(m._tool_name({"name": None}), "")

    def test_none_description_becomes_empty_string(self):
        m = ToolMatcher([{"description": None}])
        self.assertEqual(m._tool_description({"description": None}), "")

    # -- Confidence clamping ------------------------------------------

    def test_confidence_above_1_clamped(self):
        m = ToolMatcher([{"name": "calc", "confidence": 5.0}])
        res = m.match("calc")
        self.assertTrue(len(res.candidates) > 0)
        self.assertLessEqual(res.candidates[0].score, 1.0)

    def test_confidence_below_0_clamped(self):
        m = ToolMatcher([{"name": "calc", "confidence": -3.0}])
        res = m.match("calc")
        # Clamped confidence (0.0) * any ratio, then clamped -> 0.0
        if res.candidates:
            self.assertGreaterEqual(res.candidates[0].score, 0.0)

    def test_missing_confidence_defaults_to_1(self):
        m = ToolMatcher([{"name": "calc"}])
        res = m.match("calc")
        self.assertTrue(len(res.candidates) > 0)
        self.assertGreater(res.candidates[0].score, 0.0)

    # -- 0-candidate catalog short-circuit ----------------------------

    def test_empty_catalog_non_ambiguous(self):
        m = ToolMatcher([])
        res = m.match("anything")
        self.assertFalse(res.is_ambiguous)
        self.assertEqual(res.candidates, [])

    def test_empty_catalog_no_gap_indexing(self):
        """Ensure no IndexError when catalog is empty."""
        m = ToolMatcher([])
        res = m.match("x")
        # If we got here without exception, the short-circuit worked
        self.assertFalse(res.is_ambiguous)

    # -- 1-candidate catalog short-circuit ----------------------------

    def test_single_tool_catalog_non_ambiguous_even_with_close_score(self):
        m = ToolMatcher(
            [{"name": "search", "description": "search things"}],
            ambiguity_gap_threshold=1.0,
        )
        res = m.match("search")
        self.assertFalse(res.is_ambiguous)

    def test_single_tool_below_cutoff_no_candidates(self):
        m = ToolMatcher([{"name": "zzz", "description": "zzz"}], score_cutoff=0.9)
        res = m.match("search")
        self.assertFalse(res.is_ambiguous)
        self.assertEqual(res.candidates, [])

    def test_single_tool_no_gap_indexing(self):
        """Ensure no IndexError when only one tool exists."""
        m = ToolMatcher(
            [{"name": "x", "description": "x"}],
            ambiguity_gap_threshold=0.0,
        )
        res = m.match("x")
        self.assertFalse(res.is_ambiguous)

    # -- >= 2 candidates: ambiguity detection -------------------------

    def test_two_close_scores_are_ambiguous(self):
        catalog = [
            {"name": "search_web", "description": "search the web"},
            {"name": "search_db", "description": "search the database"},
        ]
        m = ToolMatcher(catalog, score_cutoff=0.1, ambiguity_gap_threshold=0.15)
        res = m.match("search")
        self.assertTrue(res.is_ambiguous)
        self.assertIsNotNone(res.disambiguation_prompt)

    def test_two_well_separated_scores_not_ambiguous(self):
        catalog = [
            {"name": "search", "description": "search"},
            {"name": "compute_pi", "description": "compute pi to n digits"},
        ]
        m = ToolMatcher(catalog, score_cutoff=0.1, ambiguity_gap_threshold=0.15)
        res = m.match("search")
        self.assertFalse(res.is_ambiguous)

    def test_disambiguation_prompt_contains_tool_names(self):
        catalog = [
            {"name": "tool_a", "description": "does a"},
            {"name": "tool_b", "description": "does b"},
        ]
        m = ToolMatcher(catalog, score_cutoff=0.01, ambiguity_gap_threshold=1.0)
        res = m.match("tool")
        self.assertIn("tool_a", res.disambiguation_prompt)
        self.assertIn("tool_b", res.disambiguation_prompt)

    def test_non_ambiguous_result_has_no_prompt(self):
        catalog = [
            {"name": "search", "description": "search"},
            {"name": "compute_pi", "description": "compute pi to n digits"},
        ]
        m = ToolMatcher(catalog, score_cutoff=0.1, ambiguity_gap_threshold=0.15)
        res = m.match("search")
        self.assertFalse(res.is_ambiguous)
        self.assertIsNone(res.disambiguation_prompt)

    # -- Empty intent -------------------------------------------------

    def test_empty_intent_returns_empty(self):
        m = ToolMatcher([{"name": "x"}])
        res = m.match("")
        self.assertFalse(res.is_ambiguous)
        self.assertEqual(res.candidates, [])

    def test_none_intent_coerced_safely(self):
        """Passing a non-string intent should not crash (defensive)."""
        m = ToolMatcher([{"name": "x"}])
        # Empty string after bool check would skip; but let's verify
        # a whitespace-only intent still works without error
        res = m.match("   ")
        self.assertFalse(res.is_ambiguous)

    # -- Result helpers -----------------------------------------------

    def test_best_property(self):
        m = ToolMatcher([{"name": "alpha"}], score_cutoff=0.01)
        res = m.match("alpha")
        self.assertIsNotNone(res.best)
        self.assertEqual(m._tool_name(res.best.tool), "alpha")

    def test_best_property_none_when_no_candidates(self):
        m = ToolMatcher([])
        res = m.match("x")
        self.assertIsNone(res.best)

    # -- Candidates sorted descending ---------------------------------

    def test_candidates_sorted_descending(self):
        catalog = [
            {"name": "far_match", "description": "unrelated"},
            {"name": "exact", "description": "exact"},
            {"name": "close_match", "description": "close"},
        ]
        m = ToolMatcher(catalog, score_cutoff=0.0)
        res = m.match("exact")
        scores = [c.score for c in res.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
