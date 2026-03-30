"""
Tests for the general-purpose RateLimiter.

Covers:
- Per-key RPS enforcement (async and sync)
- Thread-safe singleton: same instance returned across calls
- await_if_needed does not block the event loop (slot-reservation pattern)
- wait_if_needed blocks the calling thread
- record_error / record_success track consecutive errors per key
- get_stats returns per-key snapshots
- reset clears all state for a key
- Concurrent async callers serialised per-key without cross-key interference
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from gioe_libs.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_rate_limiter():
    """Reset relevant state between tests without replacing the singleton."""
    rl = RateLimiter()
    yield rl
    # Tear-down: wipe all tracked state so tests are independent
    rl._rps.clear()
    rl._last_request.clear()
    rl._consecutive_errors.clear()
    rl._total_requests.clear()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_same_instance_returned(self):
        a = RateLimiter()
        b = RateLimiter()
        assert a is b

    def test_configure_visible_on_second_instance(self):
        RateLimiter().configure("singleton-key", 42.0)
        assert RateLimiter()._get_rps("singleton-key") == 42.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfigure:
    def test_default_rps_is_one(self):
        rl = RateLimiter()
        assert rl._get_rps("unconfigured") == 1.0

    def test_configure_sets_rps(self):
        rl = RateLimiter()
        rl.configure("api", 5.0)
        assert rl._get_rps("api") == 5.0

    def test_configure_overrides_previous(self):
        rl = RateLimiter()
        rl.configure("api", 1.0)
        rl.configure("api", 10.0)
        assert rl._get_rps("api") == 10.0


# ---------------------------------------------------------------------------
# RPS enforcement — async
# ---------------------------------------------------------------------------


class TestAwaitIfNeeded:
    @pytest.mark.asyncio
    async def test_first_call_does_not_wait(self):
        rl = RateLimiter()
        key = "async-first"
        rl.configure(key, 1.0)
        start = time.time()
        await rl.await_if_needed(key)
        assert time.time() - start < 0.1

    @pytest.mark.asyncio
    async def test_second_call_waits_for_rps(self):
        rl = RateLimiter()
        key = "async-rps"
        rl.configure(key, 10.0)  # 100 ms interval
        await rl.await_if_needed(key)
        # Force _last_request to "now" so next call must wait
        rl._last_request[key] = time.time()
        start = time.time()
        await rl.await_if_needed(key)
        elapsed = time.time() - start
        assert elapsed >= 0.08  # at least ~80 ms (generous tolerance)

    @pytest.mark.asyncio
    async def test_records_last_request_timestamp(self):
        rl = RateLimiter()
        key = "async-ts"
        rl.configure(key, 100.0)
        before = time.time()
        await rl.await_if_needed(key)
        assert rl._last_request.get(key, 0) >= before

    @pytest.mark.asyncio
    async def test_increments_total_requests(self):
        rl = RateLimiter()
        key = "async-count"
        rl.configure(key, 100.0)
        await rl.await_if_needed(key)
        await rl.await_if_needed(key)
        assert rl._total_requests[key] == 2


# ---------------------------------------------------------------------------
# RPS enforcement — sync
# ---------------------------------------------------------------------------


class TestWaitIfNeeded:
    def test_first_call_does_not_wait(self):
        rl = RateLimiter()
        key = "sync-first"
        rl.configure(key, 1.0)
        start = time.time()
        rl.wait_if_needed(key)
        assert time.time() - start < 0.1

    def test_second_call_waits_for_rps(self):
        rl = RateLimiter()
        key = "sync-rps"
        rl.configure(key, 10.0)  # 100 ms interval
        rl.wait_if_needed(key)
        rl._last_request[key] = time.time()
        start = time.time()
        rl.wait_if_needed(key)
        elapsed = time.time() - start
        assert elapsed >= 0.08

    def test_increments_total_requests(self):
        rl = RateLimiter()
        key = "sync-count"
        rl.configure(key, 100.0)
        rl.wait_if_needed(key)
        rl.wait_if_needed(key)
        assert rl._total_requests[key] == 2


# ---------------------------------------------------------------------------
# record_error / record_success
# ---------------------------------------------------------------------------


class TestErrorTracking:
    def test_record_error_increments_counter(self):
        rl = RateLimiter()
        key = "err-inc"
        rl.record_error(key)
        assert rl._consecutive_errors[key] == 1

    def test_record_error_accumulates(self):
        rl = RateLimiter()
        key = "err-acc"
        for _ in range(3):
            rl.record_error(key)
        assert rl._consecutive_errors[key] == 3

    def test_record_success_resets_counter(self):
        rl = RateLimiter()
        key = "err-reset"
        for _ in range(3):
            rl.record_error(key)
        rl.record_success(key)
        assert rl._consecutive_errors[key] == 0

    def test_record_error_noop_for_unknown_key(self):
        rl = RateLimiter()
        rl.record_error("unknown-key")  # must not raise

    def test_record_success_noop_for_unknown_key(self):
        rl = RateLimiter()
        rl.record_success("unknown-key")  # must not raise
        assert rl._consecutive_errors.get("unknown-key", 0) == 0

    def test_errors_are_independent_per_key(self):
        rl = RateLimiter()
        rl.record_error("key-a")
        rl.record_error("key-a")
        rl.record_error("key-b")
        assert rl._consecutive_errors["key-a"] == 2
        assert rl._consecutive_errors["key-b"] == 1


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    def test_stats_includes_configured_key(self):
        rl = RateLimiter()
        rl.configure("stats-key", 3.0)
        stats = rl.get_stats()
        assert "stats-key" in stats

    def test_stats_rps_matches_configure(self):
        rl = RateLimiter()
        rl.configure("stats-rps", 7.0)
        assert rl.get_stats()["stats-rps"]["requests_per_second"] == 7.0

    @pytest.mark.asyncio
    async def test_stats_last_request_populated_after_call(self):
        rl = RateLimiter()
        key = "stats-last"
        rl.configure(key, 100.0)
        before = time.time()
        await rl.await_if_needed(key)
        stats = rl.get_stats()
        assert stats[key]["last_request"] is not None
        assert stats[key]["time_since_last"] is not None
        assert stats[key]["time_since_last"] >= 0

    def test_stats_consecutive_errors_reflected(self):
        rl = RateLimiter()
        key = "stats-errors"
        rl.record_error(key)
        rl.record_error(key)
        stats = rl.get_stats()
        assert stats[key]["consecutive_errors"] == 2

    @pytest.mark.asyncio
    async def test_stats_total_requests_reflected(self):
        rl = RateLimiter()
        key = "stats-total"
        rl.configure(key, 100.0)
        await rl.await_if_needed(key)
        await rl.await_if_needed(key)
        stats = rl.get_stats()
        assert stats[key]["total_requests"] == 2

    def test_stats_no_last_request_before_any_call(self):
        rl = RateLimiter()
        rl.configure("stats-fresh", 1.0)
        stats = rl.get_stats()
        assert stats["stats-fresh"]["last_request"] is None
        assert stats["stats-fresh"]["time_since_last"] is None


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_last_request(self):
        rl = RateLimiter()
        key = "reset-lr"
        rl.configure(key, 100.0)
        await rl.await_if_needed(key)
        assert key in rl._last_request
        rl.reset(key)
        assert key not in rl._last_request

    def test_reset_clears_rps_config(self):
        rl = RateLimiter()
        key = "reset-cfg"
        rl.configure(key, 5.0)
        rl.reset(key)
        assert key not in rl._rps

    def test_reset_clears_error_counter(self):
        rl = RateLimiter()
        key = "reset-err"
        rl.record_error(key)
        rl.reset(key)
        assert key not in rl._consecutive_errors

    def test_reset_clears_total_requests(self):
        rl = RateLimiter()
        key = "reset-total"
        rl._total_requests[key] = 7
        rl.reset(key)
        assert rl._total_requests.get(key, 0) == 0

    def test_reset_noop_for_unknown_key(self):
        rl = RateLimiter()
        rl.reset("totally-unknown")  # must not raise

    @pytest.mark.asyncio
    async def test_new_call_after_reset_does_not_wait(self):
        rl = RateLimiter()
        key = "reset-fresh"
        rl.configure(key, 10.0)
        await rl.await_if_needed(key)
        rl.reset(key)
        # After reset, no history → next call should not wait
        start = time.time()
        await rl.await_if_needed(key)
        assert time.time() - start < 0.05


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_callers_both_complete(self):
        """Two concurrent calls for the same key must both complete and each
        advance total_requests by 1 (2 total)."""
        rl = RateLimiter()
        key = "conc-same"
        rl.configure(key, 1000.0)

        with patch(
            "gioe_libs.rate_limiter.limiter.asyncio.sleep",
            return_value=None,
        ):
            await asyncio.gather(
                rl.await_if_needed(key),
                rl.await_if_needed(key),
            )

        assert rl._total_requests[key] == 2

    @pytest.mark.asyncio
    async def test_different_keys_do_not_block_each_other(self):
        """Calls on different keys must proceed concurrently."""
        rl = RateLimiter()
        key_a = "conc-a"
        key_b = "conc-b"
        rl.configure(key_a, 1000.0)
        rl.configure(key_b, 1000.0)

        events: list[str] = []
        original_sleep = asyncio.sleep

        async def tracked_sleep(delay: float) -> None:
            events.append("enter")
            await original_sleep(0)
            events.append("exit")

        with patch(
            "gioe_libs.rate_limiter.limiter.asyncio.sleep",
            side_effect=tracked_sleep,
        ):
            # Force both to need to sleep by setting _last_request to now
            rl._last_request[key_a] = time.time()
            rl._last_request[key_b] = time.time()
            await asyncio.gather(
                rl.await_if_needed(key_a),
                rl.await_if_needed(key_b),
            )

        assert events.count("enter") == 2
        assert events.count("exit") == 2
