"""
Riot High-Performance Rate Limiting
===================================

Token bucket implementation with:

- O(1) consume
- Monotonic clock
- Lazy cleanup
- Per-key burst capacity
- Per-operation costs
- Thread-safe updates
"""

from __future__ import annotations

import math
import time

from dataclasses import dataclass
from threading import Lock
from typing import Optional


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float
    limit: int


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    last_access_at: float


class TokenBucketLimiter:
    """
    In-process token bucket.

    Complexity
    ----------
    consume: O(1)
    prune: O(n)

    For multi-node deployments use the same interface backed by Redis.
    """

    def __init__(
        self,
        capacity: int = 60,
        refill_per_second: float = 1.0,
        *,
        idle_ttl_seconds: float = 900.0,
    ) -> None:

        if capacity <= 0:
            raise ValueError(
                "capacity must be positive"
            )

        if refill_per_second <= 0:
            raise ValueError(
                "refill_per_second must be positive"
            )

        self.capacity = float(
            capacity
        )

        self.refill_per_second = float(
            refill_per_second
        )

        self.idle_ttl_seconds = max(
            1.0,
            float(idle_ttl_seconds),
        )

        self._buckets: dict[
            str,
            _Bucket,
        ] = {}

        self._lock = Lock()

    def consume(
        self,
        key: str,
        *,
        cost: float = 1.0,
    ) -> RateLimitDecision:

        if not key:
            raise ValueError(
                "rate-limit key cannot be empty"
            )

        if not math.isfinite(
            cost
        ) or cost <= 0:
            raise ValueError(
                "cost must be positive and finite"
            )

        now = time.monotonic()

        with self._lock:

            bucket = self._buckets.get(
                key
            )

            if bucket is None:
                bucket = _Bucket(
                    tokens=self.capacity,
                    updated_at=now,
                    last_access_at=now,
                )

            elapsed = max(
                0.0,
                now - bucket.updated_at,
            )

            bucket.tokens = min(
                self.capacity,
                bucket.tokens
                + elapsed
                * self.refill_per_second,
            )

            bucket.updated_at = now
            bucket.last_access_at = now

            if bucket.tokens < cost:

                shortage = (
                    cost - bucket.tokens
                )

                retry_after = (
                    shortage
                    / self.refill_per_second
                )

                self._buckets[key] = bucket

                return RateLimitDecision(
                    allowed=False,
                    remaining=max(
                        0,
                        int(bucket.tokens),
                    ),
                    retry_after_seconds=round(
                        retry_after,
                        3,
                    ),
                    limit=int(
                        self.capacity
                    ),
                )

            bucket.tokens -= cost

            self._buckets[key] = bucket

            return RateLimitDecision(
                allowed=True,
                remaining=max(
                    0,
                    int(bucket.tokens),
                ),
                retry_after_seconds=0.0,
                limit=int(
                    self.capacity
                ),
            )

    def remaining(
        self,
        key: str,
    ) -> int:

        now = time.monotonic()

        with self._lock:
            bucket = self._buckets.get(
                key
            )

            if bucket is None:
                return int(
                    self.capacity
                )

            elapsed = max(
                0.0,
                now - bucket.updated_at,
            )

            tokens = min(
                self.capacity,
                bucket.tokens
                + elapsed
                * self.refill_per_second,
            )

            return max(
                0,
                int(tokens),
            )

    def prune(
        self,
        idle_seconds: Optional[float] = None,
    ) -> int:

        ttl = (
            self.idle_ttl_seconds
            if idle_seconds is None
            else max(
                1.0,
                idle_seconds,
            )
        )

        cutoff = (
            time.monotonic()
            - ttl
        )

        removed = 0

        with self._lock:

            stale_keys = [
                key
                for key, bucket
                in self._buckets.items()
                if bucket.last_access_at
                < cutoff
            ]

            for key in stale_keys:
                self._buckets.pop(
                    key,
                    None,
                )
                removed += 1

        return removed

    def size(self) -> int:
        with self._lock:
            return len(
                self._buckets
            )

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


__all__ = [
    "TokenBucketLimiter",
    "RateLimitDecision",
]
