"""
Tests for the general-purpose CircuitBreaker.

Covers:
- Singleton: same instance returned across calls
- Closed circuit allows calls (is_open returns False)
- Circuit opens after failure threshold is reached
- Circuit auto-resets after cooldown elapses
- record_success resets failure count and closes the circuit
- Failures during an open circuit do not extend cooldown
- Per-key isolation: independent keys do not interfere
- Concurrent access: thread-safe under parallel callers
- get_stats returns per-key snapshots
- reset clears all state for a key
"""

import threading
import time
from unittest.mock import patch

import pytest

from gioe_libs.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_circuit_breaker():
    """Reset all state between tests without replacing the singleton."""
    cb = CircuitBreaker()
    yield cb
    cb._failure_count.clear()
    cb._opened_at.clear()
    cb._failure_threshold.clear()
    cb._cooldown_seconds.clear()
    cb._key_locks.clear()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_same_instance_returned(self):
        a = CircuitBreaker()
        b = CircuitBreaker()
        assert a is b


# ---------------------------------------------------------------------------
# Closed state
# ---------------------------------------------------------------------------


class TestClosedState:
    def test_new_key_is_closed(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        assert cb.is_open("service-a") is False

    def test_below_threshold_remains_closed(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-a", failure_threshold=3, cooldown_seconds=60)
        cb.record_failure("service-a")
        cb.record_failure("service-a")
        assert cb.is_open("service-a") is False

    def test_record_success_resets_failure_count(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-a", failure_threshold=3, cooldown_seconds=60)
        cb.record_failure("service-a")
        cb.record_failure("service-a")
        cb.record_success("service-a")
        cb.record_failure("service-a")
        cb.record_failure("service-a")
        # Only 2 failures since last success — still below threshold
        assert cb.is_open("service-a") is False


# ---------------------------------------------------------------------------
# Open state (transition from closed)
# ---------------------------------------------------------------------------


class TestOpenTransition:
    def test_opens_at_threshold(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-b", failure_threshold=3, cooldown_seconds=60)
        cb.record_failure("service-b")
        cb.record_failure("service-b")
        assert cb.is_open("service-b") is False
        cb.record_failure("service-b")
        assert cb.is_open("service-b") is True

    def test_open_circuit_blocks_further_calls(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-b", failure_threshold=2, cooldown_seconds=60)
        cb.record_failure("service-b")
        cb.record_failure("service-b")
        assert cb.is_open("service-b") is True

    def test_record_success_closes_open_circuit(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-b", failure_threshold=2, cooldown_seconds=60)
        cb.record_failure("service-b")
        cb.record_failure("service-b")
        assert cb.is_open("service-b") is True
        cb.record_success("service-b")
        assert cb.is_open("service-b") is False

    def test_failures_during_open_do_not_extend_cooldown(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-b", failure_threshold=2, cooldown_seconds=0.1)

        cb.record_failure("service-b")
        cb.record_failure("service-b")
        opened_at = cb._opened_at.get("service-b")

        # Record more failures while open
        cb.record_failure("service-b")
        cb.record_failure("service-b")

        # opened_at should not have changed
        assert cb._opened_at.get("service-b") == opened_at


# ---------------------------------------------------------------------------
# Auto-reset after cooldown
# ---------------------------------------------------------------------------


class TestCooldownReset:
    def test_auto_resets_after_cooldown(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-c", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure("service-c")
        assert cb.is_open("service-c") is True

        time.sleep(0.1)
        assert cb.is_open("service-c") is False

    def test_failure_count_cleared_after_cooldown_reset(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-c", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure("service-c")
        assert cb.is_open("service-c") is True

        time.sleep(0.1)
        cb.is_open("service-c")  # triggers auto-reset
        assert cb._failure_count.get("service-c", 0) == 0

    def test_circuit_reopens_after_cooldown_if_failures_continue(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("service-c", failure_threshold=2, cooldown_seconds=0.05)
        cb.record_failure("service-c")
        cb.record_failure("service-c")
        assert cb.is_open("service-c") is True

        time.sleep(0.1)
        assert cb.is_open("service-c") is False  # auto-reset

        # New failures can open it again
        cb.record_failure("service-c")
        cb.record_failure("service-c")
        assert cb.is_open("service-c") is True


# ---------------------------------------------------------------------------
# Per-key isolation
# ---------------------------------------------------------------------------


class TestKeyIsolation:
    def test_open_circuit_does_not_affect_other_keys(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("key-x", failure_threshold=1, cooldown_seconds=60)
        cb.configure("key-y", failure_threshold=1, cooldown_seconds=60)

        cb.record_failure("key-x")
        assert cb.is_open("key-x") is True
        assert cb.is_open("key-y") is False

    def test_each_key_has_independent_failure_count(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("key-a", failure_threshold=3, cooldown_seconds=60)
        cb.configure("key-b", failure_threshold=3, cooldown_seconds=60)

        cb.record_failure("key-a")
        cb.record_failure("key-a")
        cb.record_failure("key-b")

        assert cb.is_open("key-a") is False
        assert cb.is_open("key-b") is False


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    def test_concurrent_failures_open_circuit_exactly_once(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("concurrent-key", failure_threshold=5, cooldown_seconds=60)

        errors = []

        def fail_repeatedly():
            try:
                for _ in range(10):
                    cb.record_failure("concurrent-key")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=fail_repeatedly) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cb.is_open("concurrent-key") is True

    def test_concurrent_different_keys_no_interference(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        results = {}
        errors = []

        def open_circuit(key):
            try:
                cb.configure(key, failure_threshold=3, cooldown_seconds=60)
                for _ in range(3):
                    cb.record_failure(key)
                results[key] = cb.is_open(key)
            except Exception as exc:
                errors.append(exc)

        keys = [f"svc-{i}" for i in range(20)]
        threads = [threading.Thread(target=open_circuit, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        for key in keys:
            assert results[key] is True


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_get_stats_includes_known_keys(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("stats-key", failure_threshold=3, cooldown_seconds=30)
        cb.record_failure("stats-key")

        stats = cb.get_stats()
        assert "stats-key" in stats
        s = stats["stats-key"]
        assert s["failure_count"] == 1
        assert s["failure_threshold"] == 3
        assert s["cooldown_seconds"] == 30
        assert s["is_open"] is False
        assert s["opened_at"] is None
        assert s["cooldown_remaining"] is None

    def test_get_stats_shows_open_circuit(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("stats-open", failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("stats-open")

        stats = cb.get_stats()
        s = stats["stats-open"]
        assert s["is_open"] is True
        assert s["opened_at"] is not None
        assert s["cooldown_remaining"] > 0

    def test_get_stats_consistent_after_cooldown_auto_reset(self, clean_circuit_breaker):
        """get_stats() must return an internally consistent snapshot when the
        cooldown elapses during the call: is_open=False, opened_at=None,
        cooldown_remaining=None — never a mix of is_open=False with a
        non-None opened_at."""
        cb = clean_circuit_breaker
        cb.configure("stats-reset", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure("stats-reset")
        assert cb.is_open("stats-reset") is True

        time.sleep(0.1)  # cooldown elapses before get_stats() is called

        stats = cb.get_stats()
        s = stats["stats-reset"]
        assert s["is_open"] is False
        assert s["opened_at"] is None
        assert s["cooldown_remaining"] is None


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_closes_open_circuit(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("reset-key", failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("reset-key")
        assert cb.is_open("reset-key") is True

        cb.reset("reset-key")
        assert cb.is_open("reset-key") is False

    def test_reset_clears_configuration(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("reset-key", failure_threshold=1, cooldown_seconds=60)
        cb.reset("reset-key")

        assert "reset-key" not in cb._failure_threshold
        assert "reset-key" not in cb._cooldown_seconds

    def test_reset_key_reverts_to_defaults(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("reset-key", failure_threshold=1, cooldown_seconds=60)
        cb.reset("reset-key")

        # After reset, defaults apply
        assert cb._get_threshold("reset-key") == 5
        assert cb._get_cooldown("reset-key") == 60.0

    def test_reset_removes_key_from_key_locks(self, clean_circuit_breaker):
        cb = clean_circuit_breaker
        cb.configure("reset-key", failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("reset-key")  # ensures _key_locks entry is created
        assert "reset-key" in cb._key_locks

        cb.reset("reset-key")
        assert "reset-key" not in cb._key_locks
