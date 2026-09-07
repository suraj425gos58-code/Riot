"""
circuit_breaker.py

Async circuit breaker, retry engine and provider health registry.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Tuple, TypeVar

logger = logging.getLogger("GodNode.CircuitBreaker")

T = TypeVar("T")


# ============================================================
# STATES
# ============================================================

class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ============================================================
# CONFIG
# ============================================================

@dataclass(slots=True)
class RetryConfig:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 20.0
    exponential_base: float = 2.0
    jitter: bool = True

    retry_http_codes: Tuple[int, ...] = (
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    )

    retry_exceptions: Tuple[type, ...] = (
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
    )


@dataclass(slots=True)
class CircuitConfig:
    failure_threshold: int = 5
    recovery_timeout: int = 180
    half_open_max_calls: int = 2
    success_threshold: int = 2


# ============================================================
# HEALTH
# ============================================================

@dataclass(slots=True)
class ProviderHealth:
    provider: str

    state: CircuitState = CircuitState.CLOSED

    score: float = 100.0

    successes: int = 0
    failures: int = 0
    total_requests: int = 0

    consecutive_failures: int = 0
    consecutive_successes: int = 0

    last_error: Optional[str] = None
    last_failure_time: float = 0.0
    opened_until: float = 0.0

    latency_ms: float = 0.0

    metadata: dict = field(default_factory=dict)


# ============================================================
# EXCEPTIONS
# ============================================================

class CircuitOpenError(RuntimeError):
    """Provider circuit is not currently available."""


class HalfOpenCapacityError(RuntimeError):
    """Half-open circuit probe capacity is exhausted."""


# ============================================================
# RETRY ENGINE
# ============================================================

class RetryEngine:
    def __init__(
        self,
        config: RetryConfig | None = None,
    ) -> None:
        self.config = config or RetryConfig()

        if self.config.max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")

    async def sleep(self, attempt: int) -> None:
        delay = min(
            self.config.base_delay
            * (self.config.exponential_base ** attempt),
            self.config.max_delay,
        )

        if self.config.jitter:
            delay += random.uniform(0.0, 0.5)

        await asyncio.sleep(delay)

    def should_retry_http(self, status: int) -> bool:
        return status in self.config.retry_http_codes

    def should_retry_exception(self, exc: Exception) -> bool:
        return isinstance(
            exc,
            self.config.retry_exceptions,
        )


# ============================================================
# CIRCUIT BREAKER
# ============================================================

class ProviderCircuitBreaker:
    def __init__(
        self,
        provider: str,
        config: CircuitConfig | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or CircuitConfig()

        if self.config.failure_threshold <= 0:
            raise ValueError("failure_threshold must be > 0")

        if self.config.recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be > 0")

        if self.config.half_open_max_calls <= 0:
            raise ValueError("half_open_max_calls must be > 0")

        if self.config.success_threshold <= 0:
            raise ValueError("success_threshold must be > 0")

        self.health = ProviderHealth(
            provider=provider,
        )

        self._lock = asyncio.Lock()
        self._half_open_in_flight = 0

    async def allow_request(self) -> bool:
        """
        Reserve a request slot.

        CLOSED:
            allow normally.

        OPEN:
            deny until recovery timeout.

        HALF_OPEN:
            allow only up to half_open_max_calls concurrent probes.
        """

        async with self._lock:
            now = time.monotonic()
            state = self.health.state

            if state == CircuitState.OPEN:
                if now < self.health.opened_until:
                    return False

                self.health.state = CircuitState.HALF_OPEN
                self.health.consecutive_successes = 0
                self._half_open_in_flight = 0

                state = CircuitState.HALF_OPEN

                logger.warning(
                    "%s entering HALF_OPEN",
                    self.provider,
                )

            if state == CircuitState.HALF_OPEN:
                if (
                    self._half_open_in_flight
                    >= self.config.half_open_max_calls
                ):
                    return False

                self._half_open_in_flight += 1

            return True

    async def _release_half_open_probe(self) -> None:
        if self._half_open_in_flight > 0:
            self._half_open_in_flight -= 1

    async def on_success(
        self,
        latency_ms: float = 0.0,
    ) -> None:
        async with self._lock:
            previous_state = self.health.state

            self.health.total_requests += 1
            self.health.successes += 1
            self.health.latency_ms = max(0.0, latency_ms)

            self.health.consecutive_successes += 1
            self.health.consecutive_failures = 0

            self.health.score = min(
                100.0,
                self.health.score + 2.0,
            )

            if previous_state == CircuitState.HALF_OPEN:
                await self._release_half_open_probe()

                if (
                    self.health.consecutive_successes
                    >= self.config.success_threshold
                ):
                    self.health.state = CircuitState.CLOSED
                    self.health.opened_until = 0.0
                    self.health.last_error = None

                    logger.info(
                        "%s circuit recovered and CLOSED.",
                        self.provider,
                    )

    async def on_failure(
        self,
        reason: str,
        latency_ms: float = 0.0,
    ) -> None:
        async with self._lock:
            previous_state = self.health.state

            self.health.total_requests += 1
            self.health.failures += 1
            self.health.last_error = reason
            self.health.last_failure_time = time.time()
            self.health.latency_ms = max(0.0, latency_ms)

            self.health.consecutive_failures += 1
            self.health.consecutive_successes = 0

            self.health.score = max(
                0.0,
                self.health.score - 10.0,
            )

            if previous_state == CircuitState.HALF_OPEN:
                await self._release_half_open_probe()

                self.health.state = CircuitState.OPEN
                self.health.opened_until = (
                    time.monotonic()
                    + self.config.recovery_timeout
                )

                logger.error(
                    "%s HALF_OPEN probe failed; circuit OPEN.",
                    self.provider,
                )
                return

            if (
                self.health.consecutive_failures
                >= self.config.failure_threshold
            ):
                self.health.state = CircuitState.OPEN
                self.health.opened_until = (
                    time.monotonic()
                    + self.config.recovery_timeout
                )

                logger.error(
                    "%s circuit OPEN.",
                    self.provider,
                )

    def snapshot(self) -> ProviderHealth:
        """
        Return a detached health snapshot.
        """
        return ProviderHealth(
            provider=self.health.provider,
            state=self.health.state,
            score=self.health.score,
            successes=self.health.successes,
            failures=self.health.failures,
            total_requests=self.health.total_requests,
            consecutive_failures=self.health.consecutive_failures,
            consecutive_successes=self.health.consecutive_successes,
            last_error=self.health.last_error,
            last_failure_time=self.health.last_failure_time,
            opened_until=self.health.opened_until,
            latency_ms=self.health.latency_ms,
            metadata=dict(self.health.metadata),
        )


# ============================================================
# REGISTRY
# ============================================================

class CircuitRegistry:
    def __init__(self) -> None:
        self._providers: Dict[
            str,
            ProviderCircuitBreaker,
        ] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        provider: str,
    ) -> ProviderCircuitBreaker:
        async with self._lock:
            breaker = self._providers.get(provider)

            if breaker is None:
                breaker = ProviderCircuitBreaker(provider)
                self._providers[provider] = breaker

            return breaker

    def register_sync(
        self,
        provider: str,
    ) -> ProviderCircuitBreaker:
        """
        Bootstrap-only synchronous registration.

        Runtime code should use get().
        """
        breaker = self._providers.get(provider)

        if breaker is None:
            breaker = ProviderCircuitBreaker(provider)
            self._providers[provider] = breaker

        return breaker

    def get_sync(
        self,
        provider: str,
    ) -> ProviderCircuitBreaker:
        return self.register_sync(provider)

    def get(
        self,
        provider: str,
    ) -> ProviderCircuitBreaker:
        return self.get_sync(provider)

    def health(self) -> dict[str, ProviderHealth]:
        return {
            name: breaker.snapshot()
            for name, breaker in self._providers.items()
        }

    def providers(self) -> tuple[str, ...]:
        return tuple(self._providers.keys())


# ============================================================
# EXECUTOR
# ============================================================

class ProviderExecutor:
    def __init__(
        self,
        registry: CircuitRegistry,
        retry: RetryEngine | None = None,
    ) -> None:
        self.registry = registry
        self.retry = retry or RetryEngine()

    async def execute(
        self,
        provider: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        breaker = self.registry.get(provider)

        if not await breaker.allow_request():
            raise CircuitOpenError(
                f"{provider} circuit is unavailable."
            )

        last_exception: Exception | None = None

        for attempt in range(
            self.retry.config.max_attempts
        ):
            started = time.perf_counter()

            try:
                result = await operation()

                elapsed_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                await breaker.on_success(
                    elapsed_ms
                )

                return result

            except Exception as exc:
                last_exception = exc

                elapsed_ms = (
                    time.perf_counter() - started
                ) * 1000.0

                status = getattr(
                    exc,
                    "status",
                    None,
                )

                retryable = (
                    status is not None
                    and self.retry.should_retry_http(
                        int(status)
                    )
                ) or self.retry.should_retry_exception(
                    exc
                )

                final_attempt = (
                    attempt
                    >= self.retry.config.max_attempts - 1
                )

                if final_attempt or not retryable:
                    await breaker.on_failure(
                        str(exc),
                        elapsed_ms,
                    )
                    raise

                logger.warning(
                    "%s retry %d/%d",
                    provider,
                    attempt + 1,
                    self.retry.config.max_attempts,
                )

                await self.retry.sleep(attempt)

        assert last_exception is not None
        raise last_exception


# ============================================================
# GLOBALS
# ============================================================

CIRCUIT_REGISTRY = CircuitRegistry()

RETRY_ENGINE = RetryEngine()

PROVIDER_EXECUTOR = ProviderExecutor(
    CIRCUIT_REGISTRY,
    RETRY_ENGINE,
)
