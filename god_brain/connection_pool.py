"""
connection_pool.py

Shared HTTP infrastructure and bounded async TTL/LRU cache.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

import aiohttp

logger = logging.getLogger("GodNode.ConnectionPool")

T = TypeVar("T")


# ============================================================
# CACHE
# ============================================================

@dataclass(slots=True)
class CacheEntry(Generic[T]):
    value: T
    expires_at: float


class AsyncTTLCache(Generic[T]):
    """
    Async-safe bounded LRU + TTL cache.

    Guarantees:
    - bounded number of entries
    - TTL expiration
    - LRU eviction
    - asyncio-safe access
    - explicit zero/negative TTL handling
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        max_entries: int = 512,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")

        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")

        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cache: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = asyncio.Lock()

        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    async def get(self, key: str) -> Optional[T]:
        now = time.monotonic()

        async with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._misses += 1
                return None

            if entry.expires_at <= now:
                self._cache.pop(key, None)
                self._expirations += 1
                self._misses += 1
                return None

            self._cache.move_to_end(key)
            self._hits += 1
            return entry.value

    async def set(
        self,
        key: str,
        value: T,
        ttl: Optional[int] = None,
    ) -> None:
        effective_ttl = self._ttl if ttl is None else ttl

        if effective_ttl <= 0:
            await self.delete(key)
            return

        expiration = time.monotonic() + effective_ttl

        async with self._lock:
            self._cache[key] = CacheEntry(
                value=value,
                expires_at=expiration,
            )

            self._cache.move_to_end(key)

            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
                self._evictions += 1

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    async def contains(self, key: str) -> bool:
        value = await self.get(key)
        return value is not None

    async def cleanup(self) -> int:
        now = time.monotonic()

        async with self._lock:
            expired_keys = [
                key
                for key, entry in self._cache.items()
                if entry.expires_at <= now
            ]

            for key in expired_keys:
                self._cache.pop(key, None)

            self._expirations += len(expired_keys)

            return len(expired_keys)

    async def size(self) -> int:
        async with self._lock:
            return len(self._cache)

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            return {
                "size": len(self._cache),
                "max_entries": self._max_entries,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "expirations": self._expirations,
            }


# ============================================================
# SHARED HTTP CLIENT
# ============================================================

class SharedHTTPClient:
    """
    Single process-wide aiohttp ClientSession.

    Lifecycle:
        await SharedHTTPClient.startup()
        session = SharedHTTPClient.session()
        await SharedHTTPClient.shutdown()
    """

    _session: Optional[aiohttp.ClientSession] = None
    _lock = asyncio.Lock()

    DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
        total=60,
        connect=10,
        sock_connect=10,
        sock_read=60,
    )

    @classmethod
    async def startup(cls) -> None:
        if cls._session is not None:
            return

        async with cls._lock:
            if cls._session is not None:
                return

            ssl_context = ssl.create_default_context()

            connector = aiohttp.TCPConnector(
                ssl=ssl_context,
                limit=200,
                limit_per_host=50,
                ttl_dns_cache=600,
                use_dns_cache=True,
                enable_cleanup_closed=True,
                force_close=False,
                keepalive_timeout=120,
            )

            cls._session = aiohttp.ClientSession(
                connector=connector,
                timeout=cls.DEFAULT_TIMEOUT,
                trust_env=True,
                raise_for_status=False,
                headers={
                    "User-Agent": "GodNodeV2/Enterprise",
                    "Accept": "application/json",
                },
            )

            logger.info("Shared HTTP session initialized.")

    @classmethod
    async def shutdown(cls) -> None:
        async with cls._lock:
            session = cls._session

            if session is None:
                return

            cls._session = None

            try:
                await session.close()
            except Exception:
                logger.exception("Failed to close shared HTTP session.")

            logger.info("Shared HTTP session closed.")

    @classmethod
    def session(cls) -> aiohttp.ClientSession:
        session = cls._session

        if session is None:
            raise RuntimeError(
                "SharedHTTPClient.startup() has not been called."
            )

        return session

    @classmethod
    def initialized(cls) -> bool:
        return cls._session is not None


# ============================================================
# MODEL CACHE
# ============================================================

class ModelCache:
    """
    Provider model discovery cache.

    Bounded so provider discovery cannot grow memory forever.
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        max_providers: int = 128,
    ) -> None:
        self._cache = AsyncTTLCache[list[str]](
            ttl_seconds=ttl_seconds,
            max_entries=max_providers,
        )

    async def get_models(
        self,
        provider: str,
    ) -> Optional[list[str]]:
        return await self._cache.get(provider)

    async def set_models(
        self,
        provider: str,
        models: list[str],
        ttl: Optional[int] = None,
    ) -> None:
        # Store a copy so callers cannot mutate cached state externally.
        await self._cache.set(
            provider,
            list(models),
            ttl,
        )

    async def invalidate(self, provider: str) -> None:
        await self._cache.delete(provider)

    async def clear(self) -> None:
        await self._cache.clear()

    async def cleanup(self) -> int:
        return await self._cache.cleanup()

    async def stats(self) -> dict[str, int]:
        return await self._cache.stats()


# ============================================================
# SINGLETONS
# ============================================================

MODEL_CACHE = ModelCache(
    ttl_seconds=3600,
    max_providers=128,
)

HTTP_CLIENT = SharedHTTPClient


# ============================================================
# LIFECYCLE
# ============================================================

async def startup() -> None:
    await HTTP_CLIENT.startup()


async def shutdown() -> None:
    await HTTP_CLIENT.shutdown()
