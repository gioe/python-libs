"""Tests for error_handling module: RetryConfig, ErrorHandler."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from error_handling import (
    DataError,
    ErrorHandler,
    ErrorSeverity,
    NetworkError,
    RateLimitError,
    RetryableError,
    RetryConfig,
)


# ---------------------------------------------------------------------------
# RetryConfig
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_defaults(self):
        cfg = RetryConfig()
        assert cfg.max_attempts == 3
        assert cfg.base_delay == 1.0
        assert cfg.max_delay == 60.0
        assert cfg.exponential_base == 2.0
        assert cfg.jitter is True

    def test_custom_values(self):
        cfg = RetryConfig(max_attempts=5, base_delay=0.5, max_delay=10.0, jitter=False)
        assert cfg.max_attempts == 5
        assert cfg.base_delay == 0.5
        assert cfg.max_delay == 10.0

    def test_get_delay_no_jitter(self):
        cfg = RetryConfig(base_delay=1.0, exponential_base=2.0, max_delay=60.0, jitter=False)
        assert cfg.get_delay(1) == 1.0   # 1 * 2^0
        assert cfg.get_delay(2) == 2.0   # 1 * 2^1
        assert cfg.get_delay(3) == 4.0   # 1 * 2^2

    def test_get_delay_capped_at_max(self):
        cfg = RetryConfig(base_delay=10.0, exponential_base=2.0, max_delay=15.0, jitter=False)
        assert cfg.get_delay(3) == 15.0  # 10 * 4 = 40 → capped at 15

    def test_get_delay_with_jitter_within_range(self):
        cfg = RetryConfig(base_delay=4.0, exponential_base=2.0, max_delay=60.0, jitter=True)
        for _ in range(50):
            delay = cfg.get_delay(1)
            assert 2.0 <= delay <= 4.0, f"Jittered delay {delay} outside [2.0, 4.0]"


# ---------------------------------------------------------------------------
# ErrorHandler — retry logic
# ---------------------------------------------------------------------------


class TestErrorHandlerRetry:
    def _handler(self, max_attempts=3):
        cfg = RetryConfig(max_attempts=max_attempts, base_delay=0.0, jitter=False)
        return ErrorHandler(retry_config=cfg)

    # --- success path ---

    def test_success_on_first_attempt(self):
        handler = self._handler()
        op = AsyncMock(return_value="ok")
        result = asyncio.get_event_loop().run_until_complete(
            handler.execute_with_retry(op, "test_op")
        )
        assert result == "ok"
        assert op.call_count == 1

    def test_success_clears_error_count(self):
        handler = self._handler()
        handler.error_counts["test_op"] = 2
        op = AsyncMock(return_value="ok")
        asyncio.get_event_loop().run_until_complete(
            handler.execute_with_retry(op, "test_op")
        )
        assert "test_op" not in handler.error_counts

    # --- retry on network error ---

    def test_retries_on_network_error(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=[NetworkError("timeout"), NetworkError("timeout"), "ok"])
        result = asyncio.get_event_loop().run_until_complete(
            handler.execute_with_retry(op, "test_op")
        )
        assert result == "ok"
        assert op.call_count == 3

    def test_raises_after_max_attempts(self):
        handler = self._handler(max_attempts=2)
        op = AsyncMock(side_effect=NetworkError("timeout"))
        with pytest.raises(NetworkError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert op.call_count == 2

    # --- 4xx no-retry ---

    def test_no_retry_on_404(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=NetworkError("not found", status_code=404))
        with pytest.raises(NetworkError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert op.call_count == 1

    def test_no_retry_on_400(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=NetworkError("bad request", status_code=400))
        with pytest.raises(NetworkError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert op.call_count == 1

    def test_retries_on_500(self):
        handler = self._handler(max_attempts=2)
        op = AsyncMock(side_effect=[NetworkError("server error", status_code=500), "ok"])
        result = asyncio.get_event_loop().run_until_complete(
            handler.execute_with_retry(op, "test_op")
        )
        assert result == "ok"

    # --- RateLimitError ---

    def test_retries_on_rate_limit(self):
        handler = self._handler(max_attempts=2)
        op = AsyncMock(side_effect=[RateLimitError("rate limited"), "ok"])
        result = asyncio.get_event_loop().run_until_complete(
            handler.execute_with_retry(op, "test_op")
        )
        assert result == "ok"
        assert op.call_count == 2

    def test_retry_after_respected(self):
        cfg = RetryConfig(max_attempts=2, base_delay=0.0, jitter=False)
        handler = ErrorHandler(retry_config=cfg)
        op = AsyncMock(side_effect=[RateLimitError("rate limited", retry_after=5.0), "ok"])

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("error_handling.handler.asyncio.sleep", side_effect=fake_sleep):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )

        assert sleep_calls == [5.0]

    # --- DataError retry-once ---

    def test_data_error_retried_once(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=[DataError("parse failed"), "ok"])
        result = asyncio.get_event_loop().run_until_complete(
            handler.execute_with_retry(op, "test_op")
        )
        assert result == "ok"
        assert op.call_count == 2

    def test_data_error_not_retried_twice(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=[DataError("parse failed"), DataError("parse failed again")])
        with pytest.raises(DataError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert op.call_count == 2

    # --- severity-based no-retry ---

    def test_no_retry_on_fatal(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=NetworkError("fatal", severity=ErrorSeverity.FATAL))
        with pytest.raises(NetworkError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert op.call_count == 1

    def test_no_retry_on_high(self):
        handler = self._handler(max_attempts=3)
        op = AsyncMock(side_effect=NetworkError("high severity", severity=ErrorSeverity.HIGH))
        with pytest.raises(NetworkError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert op.call_count == 1

    # --- unknown exception wrapping ---

    def test_unknown_exception_wrapped_as_data_error(self):
        handler = self._handler(max_attempts=1)
        op = AsyncMock(side_effect=ValueError("unexpected"))
        with pytest.raises(DataError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )

    # --- error tracking ---

    def test_error_count_incremented(self):
        handler = self._handler(max_attempts=2)
        op = AsyncMock(side_effect=NetworkError("fail"))
        with pytest.raises(NetworkError):
            asyncio.get_event_loop().run_until_complete(
                handler.execute_with_retry(op, "test_op")
            )
        assert handler.error_counts["test_op"] == 2

    def test_get_error_stats(self):
        handler = self._handler(max_attempts=1)
        for name in ("op_a", "op_b"):
            op = AsyncMock(side_effect=NetworkError("fail"))
            with pytest.raises(NetworkError):
                asyncio.get_event_loop().run_until_complete(
                    handler.execute_with_retry(op, name)
                )
        handler.error_counts["op_a"] = 5
        stats = handler.get_error_stats()
        assert stats["total_operations_with_errors"] == 2
        assert stats["error_counts"]["op_a"] == 5
        assert len(stats["most_problematic_operations"]) <= 5

    # --- backoff timing ---

    def test_sleep_called_between_retries(self):
        cfg = RetryConfig(max_attempts=3, base_delay=1.0, jitter=False)
        handler = ErrorHandler(retry_config=cfg)
        op = AsyncMock(side_effect=NetworkError("fail"))

        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("error_handling.handler.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(NetworkError):
                asyncio.get_event_loop().run_until_complete(
                    handler.execute_with_retry(op, "test_op")
                )

        # 3 attempts → 2 sleeps (between attempts 1→2 and 2→3)
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == 1.0  # base_delay * 2^0
        assert sleep_calls[1] == 2.0  # base_delay * 2^1
