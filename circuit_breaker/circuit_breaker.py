"""
General-purpose per-key circuit breaker.

A single CircuitBreaker singleton tracks failure rates on arbitrary string keys
and opens the circuit when a configurable threshold is exceeded. The circuit
auto-resets after a configurable cooldown period elapses.

Thread-safe: uses a threading.Lock per key so concurrent callers from different
threads are serialised per key without blocking unrelated keys.
"""

import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional

_DEFAULT_FAILURE_THRESHOLD = 5
_DEFAULT_COOLDOWN_SECONDS = 60.0


class CircuitBreaker:
    """
    Singleton circuit breaker for arbitrary string keys.

    Usage::

        cb = CircuitBreaker()
        cb.configure("my-api", failure_threshold=3, cooldown_seconds=30.0)

        if cb.is_open("my-api"):
            raise RuntimeError("Circuit open — skipping call")

        try:
            result = make_call()
            cb.record_success("my-api")
        except Exception:
            cb.record_failure("my-api")
            raise

        # Stats / cleanup
        cb.get_stats()
        cb.reset("my-api")
    """

    _instance: Optional["CircuitBreaker"] = None
    _class_lock = threading.Lock()

    def __new__(cls) -> "CircuitBreaker":
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

        # Per-key configuration
        self._failure_threshold: Dict[str, int] = {}
        self._cooldown_seconds: Dict[str, float] = {}

        # Per-key state
        self._failure_count: Dict[str, int] = defaultdict(int)
        self._opened_at: Dict[str, float] = {}  # timestamp when circuit opened

        # Per-key locks — one lock per key avoids contention across independent keys
        self._key_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)

        # Guards config reads/writes and get_stats
        self._global_lock = threading.Lock()

        self._initialized = True

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure(
        self,
        key: str,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        """Set the failure threshold and cooldown for *key*. May be called at any time."""
        with self._global_lock:
            self._failure_threshold[key] = failure_threshold
            self._cooldown_seconds[key] = cooldown_seconds

    def _get_threshold(self, key: str) -> int:
        return self._failure_threshold.get(key, _DEFAULT_FAILURE_THRESHOLD)

    def _get_cooldown(self, key: str) -> float:
        return self._cooldown_seconds.get(key, _DEFAULT_COOLDOWN_SECONDS)

    # ------------------------------------------------------------------
    # Circuit state
    # ------------------------------------------------------------------

    def is_open(self, key: str) -> bool:
        """
        Return True if the circuit for *key* is open (calls should be blocked).

        Auto-resets to closed if the cooldown period has elapsed since the
        circuit was opened.
        """
        with self._key_locks[key]:
            return self._check_open(key)

    def _check_open(self, key: str) -> bool:
        """Must be called with self._key_locks[key] held."""
        opened_at = self._opened_at.get(key)
        if opened_at is None:
            return False

        elapsed = time.time() - opened_at
        if elapsed >= self._get_cooldown(key):
            # Cooldown elapsed — auto-reset
            self._opened_at.pop(key, None)
            self._failure_count[key] = 0
            return False

        return True

    # ------------------------------------------------------------------
    # Outcome recording
    # ------------------------------------------------------------------

    def record_success(self, key: str) -> None:
        """Record a successful call: reset failure count and close the circuit."""
        with self._key_locks[key]:
            self._failure_count[key] = 0
            self._opened_at.pop(key, None)

    def record_failure(self, key: str) -> None:
        """
        Record a failed call. Opens the circuit if the failure threshold is reached.

        No-ops if the circuit is already open (failures during an open circuit
        do not extend the cooldown window).
        """
        with self._key_locks[key]:
            if self._check_open(key):
                return

            self._failure_count[key] += 1
            if self._failure_count[key] >= self._get_threshold(key):
                self._opened_at[key] = time.time()

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Dict]:
        """Return a snapshot of circuit-breaker state for all known keys."""
        with self._global_lock:
            all_keys = (
                set(self._failure_threshold)
                | set(self._failure_count)
                | set(self._opened_at)
            )

        stats: Dict[str, Dict] = {}
        for key in all_keys:
            with self._key_locks[key]:
                # Call _check_open first — it may auto-reset _opened_at[key] if
                # cooldown has elapsed. Capture opened_at afterwards so the
                # snapshot is internally consistent (is_open, opened_at, and
                # cooldown_remaining all reflect the same post-check state).
                is_open = self._check_open(key)
                opened_at = self._opened_at.get(key)
                stats[key] = {
                    "is_open": is_open,
                    "failure_count": self._failure_count.get(key, 0),
                    "failure_threshold": self._get_threshold(key),
                    "cooldown_seconds": self._get_cooldown(key),
                    "opened_at": datetime.fromtimestamp(opened_at) if opened_at else None,
                    "cooldown_remaining": max(
                        0.0,
                        self._get_cooldown(key) - (time.time() - opened_at),
                    )
                    if opened_at
                    else None,
                }
        return stats

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, key: str) -> None:
        """Clear all circuit-breaker state for *key*, including configuration."""
        with self._key_locks[key]:
            self._failure_count.pop(key, None)
            self._opened_at.pop(key, None)
        # Remove the lock after releasing it so transient keys don't accumulate
        # Lock objects indefinitely.
        with self._global_lock:
            self._failure_threshold.pop(key, None)
            self._cooldown_seconds.pop(key, None)
            self._key_locks.pop(key, None)
