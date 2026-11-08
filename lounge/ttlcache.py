"""Tiny TTL cache for *slow-changing reference data only* (season rows).

Never use it for MMR, standings or results: those must always be read fresh.
Concurrent misses for the same key share a single in-flight load.
"""

import asyncio
import time


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict = {}
        self._inflight: dict = {}

    def clear(self) -> None:
        self._data.clear()

    async def get(self, key, loader):
        hit = self._data.get(key)
        now = time.monotonic()
        if hit is not None and hit[0] > now:
            return hit[1]
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await loader()
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved; avoids "never retrieved" noise
            raise
        else:
            self._data[key] = (time.monotonic() + self.ttl, value)
            fut.set_result(value)
            return value
        finally:
            self._inflight.pop(key, None)


season_cache = TTLCache(ttl=15.0)


def invalidate_seasons() -> None:
    season_cache.clear()
