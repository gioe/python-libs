"""
General-purpose per-key rate limiter.

A single RateLimiter singleton enforces RPS-based rate limits on arbitrary
string keys.  All scraper-specific concerns (anti-detection, session rotation,
browser profiles, domain extraction) are intentionally absent.

Thread-safe: uses a threading.Lock per key so concurrent callers from different
threads or event loops are serialised per key without blocking unrelated keys.
The async path (await_if_needed) uses asyncio.sleep — it never blocks the loop.
"""

import asyncio
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional

_DEFAULT_RPS = 1.0


class RateLimiter:
    """
    Singleton rate limiter for arbitrary string keys.

    Usage::

        rl = RateLimiter()
        rl.configure("my-api", requests_per_second=5.0)

        # async context
        await rl.await_if_needed("my-api")

        # sync context
        rl.wait_if_needed("my-api")

        # outcome tracking
        rl.record_error("my-api")
        rl.record_success("my-api")

        # stats / cleanup
        rl.get_stats()
        rl.reset("my-api")
    """

    _instance: Optional["RateLimiter"] = None
    _class_lock = threading.Lock()

    def __new__(cls) -> "RateLimiter":
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        # Per-key RPS configuration (key → float)
        self._rps: Dict[str, float] = {}

        # Per-key timing state
        self._last_request: Dict[str, float] = {}
        # Per-key locks; defaultdict ensures a lock is always available.
        # Use threading.Lock (not asyncio.Lock) so callers from different
        # threads — each with their own event loop — are safe.
        self._key_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)

        # Per-key outcome tracking
        self._consecutive_errors: Dict[str, int] = {}
        self._total_requests: Dict[str, int] = defaultdict(int)

        # Guards _rps config writes and get_stats reads
        self._global_lock = threading.Lock()

        self._initialized = True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(self, key: str, requests_per_second: float) -> None:
        """Set the RPS limit for *key*.  May be called at any time."""
        with self._global_lock:
            self._rps[key] = requests_per_second

    def _get_rps(self, key: str) -> float:
        return self._rps.get(key, _DEFAULT_RPS)

    # ------------------------------------------------------------------
    # Main rate-limiting interface
    # ------------------------------------------------------------------

    async def await_if_needed(self, key: str) -> None:
        """
        Async rate limiting for *key*.

        Reserves a slot (advances _last_request inside the lock) then awaits
        asyncio.sleep outside the lock so other coroutines are not blocked.
        """
        with self._key_locks[key]:
            wait_time = self._compute_wait(key)
            self._last_request[key] = time.time() + (wait_time or 0)
            self._total_requests[key] += 1
        if wait_time:
            await asyncio.sleep(wait_time)

    def wait_if_needed(self, key: str) -> None:
        """
        Synchronous rate limiting for *key*.

        Blocks the calling thread if necessary.  Use await_if_needed from
        async contexts.
        """
        with self._key_locks[key]:
            wait_time = self._compute_wait(key)
            if wait_time:
                time.sleep(wait_time)
            self._last_request[key] = time.time()
            self._total_requests[key] += 1

    def _compute_wait(self, key: str) -> Optional[float]:
        min_interval = 1.0 / self._get_rps(key)
        now = time.time()
        last = self._last_request.get(key, 0)
        elapsed = now - last
        if elapsed < min_interval:
            return min_interval - elapsed
        return None

    # ------------------------------------------------------------------
    # Outcome tracking
    # ------------------------------------------------------------------

    def record_error(self, key: str) -> None:
        """Increment the consecutive-error counter for *key*."""
        with self._global_lock:
            self._consecutive_errors[key] = self._consecutive_errors.get(key, 0) + 1

    def record_success(self, key: str) -> None:
        """Reset the consecutive-error counter for *key* after a successful call."""
        with self._global_lock:
            self._consecutive_errors[key] = 0

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Dict]:
        """Return a snapshot of rate-limiting statistics for all known keys."""
        with self._global_lock:
            keys = (
                set(self._rps)
                | set(self._last_request)
                | set(self._consecutive_errors)
                | set(self._total_requests)
            )
            stats: Dict[str, Dict] = {}
            for key in keys:
                last = self._last_request.get(key, 0)
                stats[key] = {
                    "requests_per_second": self._get_rps(key),
                    "last_request": datetime.fromtimestamp(last) if last else None,
                    "time_since_last": time.time() - last if last else None,
                    "consecutive_errors": self._consecutive_errors.get(key, 0),
                    "total_requests": self._total_requests.get(key, 0),
                }
            return stats

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, key: str) -> None:
        """Clear all rate-limiting state for *key*."""
        with self._key_locks[key]:
            self._last_request.pop(key, None)
        with self._global_lock:
            self._rps.pop(key, None)
            self._consecutive_errors.pop(key, None)
            self._total_requests.pop(key, None)
