"""Tests for per-server CircuitBreaker state machine."""

import threading
import time
from unittest.mock import patch

import pytest

from mcp_server_manager.circuit_breaker import CircuitBreaker, CircuitState


class TestInitialisation:
    def test_starts_closed(self):
        cb = CircuitBreaker("srv-1")
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.success_count == 0
        assert cb.forced is False

    def test_custom_thresholds(self):
        cb = CircuitBreaker("srv-1", failure_threshold=3, recovery_timeout=99)
        assert cb.failure_threshold == 3
        assert cb.recovery_timeout == 99


class TestClosedState:
    def test_allow_request_when_closed(self):
        cb = CircuitBreaker("srv-1")
        assert cb.allow_request() is True

    def test_record_success_stays_closed(self):
        cb = CircuitBreaker("srv-1")
        cb.record_success()
        assert cb.state is CircuitState.CLOSED
        assert cb.success_count == 1
        assert cb.failure_count == 0

    def test_record_failure_increments_counter(self):
        cb = CircuitBreaker("srv-1", failure_threshold=3)
        cb.record_failure()
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 1

    def test_transitions_to_open_on_threshold(self):
        cb = CircuitBreaker("srv-1", failure_threshold=2)
        cb.record_failure()
        assert cb.state is CircuitState.CLOSED
        cb.record_failure()
        assert cb.state is CircuitState.OPEN

    def test_success_resets_failure_counter(self):
        cb = CircuitBreaker("srv-1", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state is CircuitState.CLOSED


class TestOpenState:
    def test_allow_request_fails_fast(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1)
        cb.record_failure()
        assert cb.allow_request() is False

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        time.sleep(0.02)
        assert cb.allow_request() is True
        assert cb.state is CircuitState.HALF_OPEN

    def test_no_transition_before_timeout(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=10)
        cb.record_failure()
        assert cb.allow_request() is False
        assert cb.state is CircuitState.OPEN

    def test_record_failure_in_open_is_noop(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1)
        cb.record_failure()
        count_before = cb.failure_count
        cb.record_failure()
        assert cb.failure_count == count_before
        assert cb.state is CircuitState.OPEN

    def test_record_success_in_open_is_noop(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1)
        cb.record_failure()
        cb.record_success()
        assert cb.state is CircuitState.OPEN


class TestHalfOpenState:
    def test_single_probe_admitted(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        # First caller gets the probe
        assert cb.allow_request() is True
        assert cb.state is CircuitState.HALF_OPEN
        # Second concurrent caller is rejected
        assert cb.allow_request() is False

    def test_probe_success_transitions_to_closed(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()
        cb.record_success()
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_probe_failure_transitions_to_open(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()
        cb.record_failure()
        assert cb.state is CircuitState.OPEN

    def test_probe_failure_resets_timeout(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()
        cb.record_failure()
        # Should not immediately go to half-open again
        assert cb.allow_request() is False
        assert cb.state is CircuitState.OPEN


class TestHalfOpenAtomicity:
    def test_concurrent_callers_see_consistent_state(self):
        """Only one thread gets the probe; all others get OPEN rejection."""
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)

        results = []
        barrier = threading.Barrier(10)

        def try_request():
            barrier.wait()
            results.append(cb.allow_request())

        threads = [threading.Thread(target=try_request) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1  # exactly one True
        assert results.count(False) == 9
        assert cb.state is CircuitState.HALF_OPEN


class TestForceOpen:
    def test_force_open_from_closed(self):
        cb = CircuitBreaker("srv-1")
        cb.force_open()
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True

    def test_force_open_from_half_open(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()  # -> HALF_OPEN
        cb.force_open()
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True

    def test_force_open_prevents_timeout_to_half_open(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.force_open()
        time.sleep(0.02)
        assert cb.allow_request() is False
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True

    def test_force_open_makes_record_failure_noop(self):
        cb = CircuitBreaker("srv-1")
        cb.force_open()
        before = cb.failure_count
        cb.record_failure()
        assert cb.failure_count == before

    def test_force_open_makes_record_success_noop(self):
        cb = CircuitBreaker("srv-1")
        cb.force_open()
        before_success = cb.success_count
        cb.record_success()
        assert cb.success_count == before_success
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True

    def test_inflight_cannot_override_force_open(self):
        """A request admitted before force_open cannot flip state back."""
        cb = CircuitBreaker("srv-1")
        assert cb.allow_request() is True  # request in-flight
        cb.force_open()
        # In-flight request completes with success — should NOT change state
        cb.record_success()
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True

    def test_inflight_failure_cannot_worsen_force_open(self):
        cb = CircuitBreaker("srv-1")
        assert cb.allow_request() is True
        cb.force_open()
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True
        # failure_count should not have been incremented
        assert cb.failure_count == 0


class TestForceClose:
    def test_force_close_clears_forced_flag(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1)
        cb.record_failure()  # natural OPEN
        cb.force_open()      # forced OPEN
        cb.force_close()
        assert cb.state is CircuitState.CLOSED
        assert cb.forced is False

    def test_force_close_from_natural_open(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1)
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        cb.force_close()
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.forced is False

    def test_force_close_from_half_open(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()  # HALF_OPEN
        cb.force_close()
        assert cb.state is CircuitState.CLOSED
        assert cb.forced is False

    def test_force_close_resets_counters(self):
        cb = CircuitBreaker("srv-1", failure_threshold=5)
        for _ in range(3):
            cb.record_failure()
        cb.force_close()
        assert cb.failure_count == 0
        assert cb.success_count == 0


class TestForceImmutability:
    def test_forced_open_survives_multiple_timeout_periods(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.force_open()
        for _ in range(5):
            time.sleep(0.02)
            assert cb.allow_request() is False
            assert cb.state is CircuitState.OPEN
            assert cb.forced is True

    def test_force_open_idempotent(self):
        cb = CircuitBreaker("srv-1")
        cb.force_open()
        cb.force_open()
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True

    def test_force_close_idempotent(self):
        cb = CircuitBreaker("srv-1")
        cb.force_close()
        cb.force_close()
        assert cb.state is CircuitState.CLOSED
        assert cb.forced is False


class TestServerIsolation:
    def test_different_servers_independent(self):
        cb_a = CircuitBreaker("server-a", failure_threshold=1)
        cb_b = CircuitBreaker("server-b", failure_threshold=1)
        cb_a.record_failure()
        assert cb_a.state is CircuitState.OPEN
        assert cb_b.state is CircuitState.CLOSED

    def test_force_open_one_server_doesnt_affect_other(self):
        cb_a = CircuitBreaker("server-a")
        cb_b = CircuitBreaker("server-b")
        cb_a.force_open()
        assert cb_a.state is CircuitState.OPEN
        assert cb_a.forced is True
        assert cb_b.state is CircuitState.CLOSED
        assert cb_b.forced is False


class TestEdgeCases:
    def test_zero_failure_threshold(self):
        cb = CircuitBreaker("srv-1", failure_threshold=0)
        # First failure immediately opens
        cb.record_failure()
        assert cb.state is CircuitState.OPEN

    def test_very_long_recovery_timeout(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=999999)
        cb.record_failure()
        assert cb.allow_request() is False

    def test_success_in_closed_with_no_failures(self):
        cb = CircuitBreaker("srv-1")
        cb.record_success()
        assert cb.success_count == 1
        assert cb.failure_count == 0

    def test_alternating_success_failure_in_closed(self):
        cb = CircuitBreaker("srv-1", failure_threshold=3)
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        cb.record_failure()
        cb.record_failure()
        assert cb.state is CircuitState.OPEN

    def test_half_open_probe_then_force_open_midflight(self):
        cb = CircuitBreaker("srv-1", failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()  # probe admitted, state HALF_OPEN
        cb.force_open()     # operator overrides mid-flight
        cb.record_success() # probe result arrives — must be no-op
        assert cb.state is CircuitState.OPEN
        assert cb.forced is True


class TestProperties:
    def test_server_name_property(self):
        cb = CircuitBreaker("my-server")
        assert cb.server_name == "my-server"

    def test_state_property_matches_internal(self):
        cb = CircuitBreaker("srv-1")
        cb.force_open()
        assert cb.state is CircuitState.OPEN
        cb.force_close()
        assert cb.state is CircuitState.CLOSED
