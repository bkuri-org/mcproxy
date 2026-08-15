import pytest
from unittest.mock import patch, MagicMock, PropertyMock
import time

# ---------------------------------------------------------------------------
# Step 0: Verify sandbox.pool is a network connection pool, not a subprocess pool
# ---------------------------------------------------------------------------
import importlib
pool_mod = importlib.import_module("sandbox.pool")
PoolCls = getattr(pool_mod, "ConnectionPool", getattr(pool_mod, "Pool", None))
assert PoolCls is not None, "sandbox.pool must expose ConnectionPool or Pool"

# Reject subprocess-pool semantics
for _attr in ("_workers", "_processes", "_executor"):
    assert not hasattr(PoolCls, _attr), (
        f"sandbox.pool.{PoolCls.__name__} looks like a subprocess/thread pool (has {_attr}); "
        "retarget to the network connection pool module"
    )

# Expect network-pool semantics
assert hasattr(PoolCls, "get") or hasattr(PoolCls, "acquire"), (
    f"{PoolCls.__name__} must expose get/acquire for connection retrieval"
)
assert hasattr(PoolCls, "return_connection") or hasattr(PoolCls, "release"), (
    f"{PoolCls.__name__} must expose return_connection/release"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_pool(min_size=2, max_size=10, idle_timeout=30.0):
    return PoolCls(min_size=min_size, max_size=max_size, idle_timeout=idle_timeout)


def _make_cache_stats(avg_ttl=60.0, hit_rate=0.8, size=100, misses=20, hits=80):
    return {"avg_ttl": avg_ttl, "hit_rate": hit_rate, "size": size,
            "misses": misses, "hits": hits}


def _make_conn(state="IDLE", last_used=None):
    conn = MagicMock()
    conn.state = state
    conn.last_used = last_used or time.time()
    conn.close = MagicMock()
    return conn


# ---------------------------------------------------------------------------
# 1. adapt() with validated cache_stats
# ---------------------------------------------------------------------------
class TestAdaptCacheStatsValidation:
    """adapt() must reject malformed cache_stats."""

    def test_malformed_stats_raises(self):
        pool = _make_pool()
        with pytest.raises((ValueError, TypeError)):
            pool.adapt("not-a-dict")

    def test_missing_avg_ttl_key_raises(self):
        pool = _make_pool()
        with pytest.raises(ValueError):
            pool.adapt({"hit_rate": 0.5})

    def test_non_numeric_avg_ttl_raises(self):
        pool = _make_pool()
        with pytest.raises((TypeError, ValueError)):
            pool.adapt(_make_cache_stats(avg_ttl="abc"))

    def test_negative_avg_ttl_raises(self):
        pool = _make_pool()
        with pytest.raises(ValueError):
            pool.adapt(_make_cache_stats(avg_ttl=-5))

    def test_extra_unexpected_keys_ignored(self):
        """Unknown scalar keys are ignored; valid keys still applied."""
        pool = _make_pool(idle_timeout=30.0)
        stats = _make_cache_stats(avg_ttl=120.0)
        stats["bogus_key"] = [1, 2, 3]  # non-scalar, should be ignored
        pool.adapt(stats)
        assert pool.idle_timeout == 120.0

    def test_non_scalar_values_in_stats_rejected(self):
        """Values that are not plain scalars (int/float/None) cause rejection."""
        pool = _make_pool()
        stats = _make_cache_stats(avg_ttl=60.0)
        stats["hit_rate"] = {"nested": "dict"}
        with pytest.raises((ValueError, TypeError)):
            pool.adapt(stats)


# ---------------------------------------------------------------------------
# 2. avg_ttl None / < 1 falls back to existing idle_timeout
# ---------------------------------------------------------------------------
class TestAdaptTTLFallback:
    def test_avg_ttl_none_fallback(self):
        pool = _make_pool(idle_timeout=45.0)
        pool.adapt(_make_cache_stats(avg_ttl=None))
        assert pool.idle_timeout == 45.0

    def test_avg_ttl_zero_fallback(self):
        pool = _make_pool(idle_timeout=45.0)
        pool.adapt(_make_cache_stats(avg_ttl=0))
        assert pool.idle_timeout == 45.0

    def test_avg_ttl_fraction_below_one_fallback(self):
        pool = _make_pool(idle_timeout=45.0)
        pool.adapt(_make_cache_stats(avg_ttl=0.5))
        assert pool.idle_timeout == 45.0

    def test_avg_ttl_exactly_one_accepted(self):
        pool = _make_pool(idle_timeout=45.0)
        pool.adapt(_make_cache_stats(avg_ttl=1.0))
        assert pool.idle_timeout == 1.0

    def test_avg_ttl_valid_overrides(self):
        pool = _make_pool(idle_timeout=45.0)
        pool.adapt(_make_cache_stats(avg_ttl=90.0))
        assert pool.idle_timeout == 90.0


# ---------------------------------------------------------------------------
# 3. Hysteresis-based sizing with max(1, …) floor
# ---------------------------------------------------------------------------
class TestHysteresisSizing:
    def test_shrink_never_empties_pool(self):
        """Even with extremely low hit_rate the pool size floors at 1."""
        pool = _make_pool(min_size=1, max_size=10)
        pool.adapt(_make_cache_stats(avg_ttl=60.0, hit_rate=0.0, size=0))
        assert pool.max_size >= 1

    def test_grow_within_cap(self):
        pool = _make_pool(min_size=1, max_size=10)
        pool.adapt(_make_cache_stats(avg_ttl=60.0, hit_rate=1.0, size=1000))
        assert pool.max_size <= pool._original_max_size  # capped

    def test_hysteresis_prevents_oscillation(self):
        """Two consecutive adapts with similar stats must not flip-flop size."""
        pool = _make_pool(min_size=1, max_size=10)
        stats = _make_cache_stats(avg_ttl=60.0, hit_rate=0.45, size=50)
        pool.adapt(stats)
        size_a = pool.max_size
        pool.adapt(stats)
        size_b = pool.max_size
        assert size_a == size_b

    def test_hysteresis_band_requires_delta(self):
        """A tiny change inside the dead-band does not trigger resize."""
        pool = _make_pool(min_size=1, max_size=10)
        pool.adapt(_make_cache_stats(avg_ttl=60.0, hit_rate=0.50, size=50))
        size_before = pool.max_size
        pool.adapt(_make_cache_stats(avg_ttl=60.0, hit_rate=0.51, size=51))
        assert pool.max_size == size_before


# ---------------------------------------------------------------------------
# 4. Drain-barrier shrink: only IDLE connections are reaped
# ---------------------------------------------------------------------------
class TestDrainBarrierShrink:
    def _seed_pool(self, pool, idle=3, active=2):
        """Manually inject mock connections with known states."""
        pool._conns = []
        for _ in range(idle):
            pool._conns.append(_make_conn(state="IDLE"))
        for _ in range(active):
            pool._conns.append(_make_conn(state="ACTIVE"))

    def test_shrink_only_reaps_idle(self):
        pool = _make_pool(min_size=1, max_size=10)
        self._seed_pool(pool, idle=4, active=3)
        pool._shrink_to(target=2)
        active_remaining = [c for c in pool._conns if c.state == "ACTIVE"]
        assert len(active_remaining) == 3
        assert all(c.close.called is False for c in active_remaining)

    def test_shrink_does_not_close_active(self):
        pool = _make_pool(min_size=1, max_size=10)
        self._seed_pool(pool, idle=5, active=2)
        pool._shrink_to(target=1)
        active = [c for c in pool._conns if c.state == "ACTIVE"]
        assert len(active) == 2
        assert all(c.close.called is False for c in active)

    def test_shrink_respects_floor(self):
        pool = _make_pool(min_size=1, max_size=10)
        pool._conns = [_make_conn(state="IDLE") for _ in range(3)]
        pool._shrink_to(target=0)
        assert len(pool._conns) >= 1

    def test_no_reap_when_already_at_or_below_target(self):
        pool = _make_pool(min_size=1, max_size=10)
        self._seed_pool(pool, idle=1, active=0)
        pool._shrink_to(target=2)
        assert len(pool._conns) == 1
        assert pool._conns[0].close.called is False


# ---------------------------------------------------------------------------
# 5. Pre-warm: exact-match registry lookup, no fallback derivation
# ---------------------------------------------------------------------------
class TestPreWarmExactMatch:
    def _make_registry(self, mapping):
        reg = MagicMock()
        reg.lookup.side_effect = lambda k: mapping.get(k)
        return reg

    def test_exact_match_returns_host(self):
        reg = self._make_registry({"api.example.com": "10.0.0.1"})
        pool = _make_pool()
        host = pool._prewarm_resolve("api.example.com", registry=reg)
        assert host == "10.0.0.1"

    def test_unmapped_key_returns_none(self):
        reg = self._make_registry({"api.example.com": "10.0.0.1"})
        pool = _make_pool()
        host = pool._prewarm_resolve("unknown.host", registry=reg)
        assert host is None

    def test_prefix_does_not_match(self):
        """Subdomain prefix of a registered key must NOT match."""
        reg = self._make_registry({"example.com": "10.0.0.1"})
        pool = _make_pool()
        host = pool._prewarm_resolve("sub.example.com", registry=reg)
        assert host is None

    def test_superdomain_does_not_match(self):
        reg = self._make_registry({"sub.example.com": "10.0.0.2"})
        pool = _make_pool()
        host = pool._prewarm_resolve("example.com", registry=reg)
        assert host is None

    def test_unmapped_opens_zero_connections(self):
        reg = self._make_registry({})
        pool = _make_pool()
        pool._prewarm(["no.such.host"], registry=reg)
        assert len(pool._conns) == 0

    def test_allowlisted_only(self):
        """Only hosts present in registry are contacted; others silently skipped."""
        reg = self._make_registry({"allowed.com": "10.0.0.3"})
        pool = _make_pool()
        pool._prewarm(["allowed.com", "evil.com"], registry=reg)
        # At least one connection for allowed.com; none for evil.com
        allowed_conns = [c for c in pool._conns
                         if getattr(c, '_host', None) == "allowed.com"]
        evil_conns = [c for c in pool._conns
                      if getattr(c, '_host', None) == "evil.com"]
        assert len(allowed_conns) >= 1
        assert len(evil_conns) == 0

    def test_none_result_skipped_no_fallback_dns(self):
        """When registry returns None the pool must NOT fall back to DNS."""
        reg = self._make_registry({})
        pool = _make_pool()
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not be called")):
            host = pool._prewarm_resolve("any.host", registry=reg)
        assert host is None


# ---------------------------------------------------------------------------
# 6. Aggregate-scalar-only stats fed by http_backend.py
# ---------------------------------------------------------------------------
class TestAggregateScalarStats:
    """Pool stats must be plain scalars (int/float/None), no nested structures."""

    def test_stats_are_scalar_only(self):
        pool = _make_pool()
        stats = pool.stats()
        for key, value in stats.items():
            assert isinstance(value, (int, float, type(None))), (
                f"stats[{key!r}] = {value!r} is not a scalar"
            )

    def test_stats_keys_expected(self):
        pool = _make_pool()
        stats = pool.stats()
        expected = {"size", "idle", "active", "idle_timeout", "max_size", "min_size"}
        assert expected.issubset(set(stats.keys()))

    def test_stats_reflect_http_backend_feeds(self):
        """Simulate http_backend feeding cache_stats into adapt(), then check pool stats."""
        pool = _make_pool(idle_timeout=30.0)
        backend_stats = _make_cache_stats(avg_ttl=120.0, hit_rate=0.9, size=200, hits=180, misses=20)
        pool.adapt(backend_stats)
        stats = pool.stats()
        assert stats["idle_timeout"] == 120.0
        # Hysteresis should have grown the pool toward max
        assert stats["max_size"] >= pool.min_size

    def test_no_lists_or_dicts_in_stats(self):
        pool = _make_pool()
        stats = pool.stats()
        for value in stats.values():
            assert not isinstance(value, (list, dict, set, tuple)), (
                f"Non-scalar value in stats: {value!r}"
            )
