"""Tests for the TieredCache system (namespace / session / global tiers)."""

from __future__ import annotations

import pytest

from cache.tiered import TieredCache, TierConfig, TierName


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store() -> dict[str, object]:
    """Return a plain dict usable as a minimal backend store."""
    return {}


def _make_cache(
    *,
    ns_store: dict[str, object] | None = None,
    session_store: dict[str, object] | None = None,
    global_store: dict[str, object] | None = None,
    ns_config: TierConfig | None = None,
    session_config: TierConfig | None = None,
    global_config: TierConfig | None = None,
) -> TieredCache:
    from cache.tiered import DictTierBackend

    ns = DictTierBackend(ns_store or _make_store())
    session = DictTierBackend(session_store or _make_store())
    glob = DictTierBackend(global_store or _make_store())
    return TieredCache(
        namespace=ns,
        session=session,
        global_=glob,
        ns_config=ns_config or TierConfig(),
        session_config=session_config or TierConfig(),
        global_config=global_config or TierConfig(),
    )


# ---------------------------------------------------------------------------
# String-key encoding
# ---------------------------------------------------------------------------

class TestStringKeyEncoding:
    """Verify collision-free (context_key, key) → string encoding rules."""

    def test_none_context_encodes_with_sentinel(self) -> None:
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key(None, "foo") == "1:\x00foo"

    def test_empty_string_context(self) -> None:
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key("", "foo") == "0:foo"

    def test_normal_context(self) -> None:
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key("abc", "foo") == "3:abcfoo"

    def test_empty_key(self) -> None:
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key("ns", "") == "2:ns"

    def test_none_context_and_empty_key(self) -> None:
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key(None, "") == "1:\x00"

    def test_no_collision_between_none_and_empty_context(self) -> None:
        c = TieredCache.__new__(TieredCache)
        # None → "1:\x00{k}", empty string → "0:{k}" — always distinct
        assert c._encode_key(None, "k") != c._encode_key("", "k")

    def test_no_collision_between_different_contexts(self) -> None:
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key("a", "key") != c._encode_key("ab", "ey")

    def test_context_with_null_byte_raises(self) -> None:
        c = TieredCache.__new__(TieredCache)
        with pytest.raises(ValueError, match="\\\\x00"):
            c._encode_key("bad\x00key", "val")

    def test_key_may_contain_null_byte(self) -> None:
        """Only the context_key is validated for the sentinel."""
        c = TieredCache.__new__(TieredCache)
        assert c._encode_key("ok", "k\x00y") == "2:okk\x00y"


# ---------------------------------------------------------------------------
# TierConfig
# ---------------------------------------------------------------------------

class TestTierConfig:
    def test_defaults(self) -> None:
        cfg = TierConfig()
        assert cfg.max_items is None
        assert cfg.ttl_seconds is None
        assert cfg.enabled is True

    def test_custom(self) -> None:
        cfg = TierConfig(max_items=100, ttl_seconds=60, enabled=False)
        assert cfg.max_items == 100
        assert cfg.ttl_seconds == 60
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Basic get / set per tier
# ---------------------------------------------------------------------------

class TestBasicOperations:
    def test_set_and_get_namespace(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "v")
        assert tc.get("ns", "k") == "v"

    def test_set_and_get_session(self) -> None:
        tc = _make_cache()
        tc.set("sess", "k", "v")
        assert tc.get("sess", "k") == "v"

    def test_set_and_get_global(self) -> None:
        tc = _make_cache()
        tc.set(None, "k", "v")
        assert tc.get(None, "k") == "v"

    def test_get_miss_returns_none(self) -> None:
        tc = _make_cache()
        assert tc.get("ns", "missing") is None

    def test_delete(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "v")
        tc.delete("ns", "k")
        assert tc.get("ns", "k") is None

    def test_delete_nonexistent_is_noop(self) -> None:
        tc = _make_cache()
        tc.delete("ns", "nope")  # should not raise


# ---------------------------------------------------------------------------
# Fallback logic
# ---------------------------------------------------------------------------

class TestFallback:
    """During get(), tiers are consulted namespace → session → global."""

    def test_returns_from_namespace_and_skips_lower(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "ns-val")
        tc.set("sess", "k", "sess-val")
        tc.set(None, "k", "glob-val")
        assert tc.get("ns", "k") == "ns-val"

    def test_falls_to_session_when_namespace_misses(self) -> None:
        tc = _make_cache()
        tc.set("sess", "k", "sess-val")
        tc.set(None, "k", "glob-val")
        assert tc.get("ns", "k") == "sess-val"

    def test_falls_to_global_when_both_upper_miss(self) -> None:
        tc = _make_cache()
        tc.set(None, "k", "glob-val")
        assert tc.get("ns", "k") == "glob-val"

    def test_all_tiers_miss(self) -> None:
        tc = _make_cache()
        assert tc.get("ns", "k") is None

    def test_session_context_does_not_see_namespace_data(self) -> None:
        tc = _make_cache()
        tc.set("ns-a", "k", "v")
        # session context "ns-a" is different from namespace context "ns-a"
        # because they live in different tier backends.
        # A session-tier set would be needed to hit session.
        assert tc.get("ns-b", "k") is None


# ---------------------------------------------------------------------------
# Per-tier hit / miss stats
# ---------------------------------------------------------------------------

class TestPerTierStats:
    def test_single_tier_hit(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "v")
        tc.get("ns", "k")
        stats = tc.stats()
        assert stats[TierName.NAMESPACE]["hits"] == 1
        assert stats[TierName.NAMESPACE]["misses"] == 0

    def test_miss_then_hit_in_same_tier(self) -> None:
        tc = _make_cache()
        tc.get("ns", "k")  # miss
        tc.set("ns", "k", "v")
        tc.get("ns", "k")  # hit
        stats = tc.stats()
        assert stats[TierName.NAMESPACE]["hits"] == 1
        assert stats[TierName.NAMESPACE]["misses"] == 1

    def test_fallback_records_miss_on_upper_and_hit_on_lower(self) -> None:
        tc = _make_cache()
        tc.set(None, "k", "glob-val")
        tc.get("ns", "k")  # ns miss → sess miss → global hit
        stats = tc.stats()
        assert stats[TierName.NAMESPACE]["misses"] == 1
        assert stats[TierName.SESSION]["misses"] == 1
        assert stats[TierName.GLOBAL]["hits"] == 1

    def test_full_fallback_miss_records_all_misses(self) -> None:
        tc = _make_cache()
        tc.get("ns", "k")
        stats = tc.stats()
        for tier in TierName:
            assert stats[tier]["misses"] == 1
            assert stats[tier]["hits"] == 0

    def test_stats_independent_per_tier(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "v")
        tc.get("ns", "k")  # ns hit
        tc.get("sess", "other")  # ns miss, sess miss, global miss
        stats = tc.stats()
        assert stats[TierName.NAMESPACE]["hits"] == 1
        assert stats[TierName.NAMESPACE]["misses"] == 1
        assert stats[TierName.SESSION]["misses"] == 2  # first call miss + second call miss
        assert stats[TierName.GLOBAL]["misses"] == 2

    def test_reset_stats(self) -> None:
        tc = _make_cache()
        tc.get("ns", "k")
        tc.reset_stats()
        stats = tc.stats()
        for tier in TierName:
            assert stats[tier]["hits"] == 0
            assert stats[tier]["misses"] == 0


# ---------------------------------------------------------------------------
# Disabled tier is skipped entirely
# ---------------------------------------------------------------------------

class TestDisabledTier:
    def test_disabled_namespace_skipped(self) -> None:
        tc = _make_cache(ns_config=TierConfig(enabled=False))
        tc.set("ns", "k", "v")  # stored but tier disabled → should not be consulted
        result = tc.get("ns", "k")
        assert result is None
        stats = tc.stats()
        # namespace should not be consulted at all
        assert TierName.NAMESPACE not in stats or stats[TierName.NAMESPACE]["misses"] == 0

    def test_disabled_session_skipped(self) -> None:
        tc = _make_cache(
            session_config=TierConfig(enabled=False),
        )
        tc.set("sess", "k", "v")
        tc.set(None, "k", "glob-val")
        # ns miss → session skipped → global hit
        assert tc.get("ns", "k") == "glob-val"
        stats = tc.stats()
        assert stats[TierName.SESSION]["misses"] == 0
        assert stats[TierName.GLOBAL]["hits"] == 1

    def test_all_tiers_disabled_returns_none(self) -> None:
        tc = _make_cache(
            ns_config=TierConfig(enabled=False),
            session_config=TierConfig(enabled=False),
            global_config=TierConfig(enabled=False),
        )
        tc.set("ns", "k", "v")
        assert tc.get("ns", "k") is None


# ---------------------------------------------------------------------------
# Admin auth integration (stats exposure)
# ---------------------------------------------------------------------------

class TestAdminStatsExposure:
    """Stats must be accessible via the existing admin-route auth mechanism."""

    def test_stats_returns_dict_of_dicts(self) -> None:
        tc = _make_cache()
        stats = tc.stats()
        assert isinstance(stats, dict)
        for tier in TierName:
            assert isinstance(stats[tier], dict)
            assert "hits" in stats[tier]
            assert "misses" in stats[tier]

    def test_admin_stats_view_renders(self) -> None:
        """Simulate what the admin route does: call cache.stats()."""
        tc = _make_cache()
        tc.get("ns", "k")
        payload = tc.stats()
        # The admin route would JSON-serialize this; ensure it's serializable.
        import json
        serialized = json.dumps(payload)
        assert "namespace" in serialized
        assert '"misses": 1' in serialized


# ---------------------------------------------------------------------------
# Tuple-key isolation (collision freedom)
# ---------------------------------------------------------------------------

class TestCollisionFreedom:
    """Different (context_key, key) pairs must never map to the same string."""

    @pytest.mark.parametrize(
        "pairs",
        [
            [(None, "k"), ("", "k")],
            [("a", "bc"), ("ab", "c")],
            [("", ""), (None, "")],
            [("x" * 100, "k"), ("x" * 99 + "y", "k")],
        ],
    )
    def test_pairs_produce_distinct_keys(self, pairs: list) -> None:
        c = TieredCache.__new__(TieredCache)
        encoded = [c._encode_key(ctx, key) for ctx, key in pairs]
        assert len(set(encoded)) == len(encoded), f"collision: {encoded}"

    def test_round_trip_isolation_in_cache(self) -> None:
        tc = _make_cache()
        tc.set(None, "k", "global-value")
        tc.set("", "k", "empty-ns-value")
        tc.set("ns", "k", "ns-value")
        assert tc.get(None, "k") == "global-value"
        assert tc.get("", "k") == "empty-ns-value"
        assert tc.get("ns", "k") == "ns-value"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_overwrite_replaces_value(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "v1")
        tc.set("ns", "k", "v2")
        assert tc.get("ns", "k") == "v2"

    def test_delete_only_affects_correct_tier(self) -> None:
        tc = _make_cache()
        tc.set("ns", "k", "ns-v")
        tc.set(None, "k", "glob-v")
        tc.delete("ns", "k")
        # namespace miss → session miss → global hit
        assert tc.get("ns", "k") == "glob-v"

    def test_context_key_with_null_byte_rejected_on_set(self) -> None:
        tc = _make_cache()
        with pytest.raises(ValueError, match="\\\\x00"):
            tc.set("bad\x00ns", "k", "v")

    def test_context_key_with_null_byte_rejected_on_get(self) -> None:
        tc = _make_cache()
        with pytest.raises(ValueError, match="\\\\x00"):
            tc.get("bad\x00ns", "k")

    def test_context_key_with_null_byte_rejected_on_delete(self) -> None:
        tc = _make_cache()
        with pytest.raises(ValueError, match="\\\\x00"):
            tc.delete("bad\x00ns", "k")
