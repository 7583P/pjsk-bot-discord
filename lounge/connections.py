"""Shared connection policy for the modern bot and its separate API process."""

import asyncio
from contextlib import asynccontextmanager
import logging
import random
import ssl
import os
import asyncpg
from lounge.config import Settings, resolve_ca_file
from lounge.diagnostics import database_fingerprint
from lounge import perf

log = logging.getLogger(__name__)

_bot_pool = None
_pool_lock = asyncio.Lock()


async def create_pool(*, api=False, settings=None):
    global _bot_pool
    if _bot_pool is not None and not getattr(_bot_pool, "_closed", True):
        log.info("Reutilizando pool asyncpg existente para proceso compartido")
        return _bot_pool

    async with _pool_lock:
        if _bot_pool is not None and not getattr(_bot_pool, "_closed", True):
            return _bot_pool

        settings = settings or Settings.load(api=api)
        staging = os.getenv("PJSK_ENV") == "staging"
        expected = os.getenv("STAGING_DATABASE_FINGERPRINT")
        if staging and not expected:
            raise ValueError("Staging requires the reviewed staging database fingerprint")
        ca_file = settings.ca_file or resolve_ca_file()
        tls = ssl.create_default_context(cafile=ca_file) if settings.ssl else False
        
        # Limite conservador estricto: max_size=3 para no exceder pool_size=15 de Supabase Free
        pool_max = min(settings.pool_max, 3)
        
        for attempt in range(5):
            try:
                pool = await asyncpg.create_pool(
                    settings.database_url,
                    connection_class=perf.timed_connection_class(asyncpg.Connection),
                    min_size=1,
                    max_size=pool_max,
                    ssl=tls,
                    timeout=10,
                    command_timeout=15,
                    max_inactive_connection_lifetime=60,
                    statement_cache_size=0 if settings.pool_mode == "transaction" else 100,
                )
                if staging:
                    try:
                        async with pool.acquire() as c:
                            if (
                                await database_fingerprint(c, settings.database_url)
                                != expected
                            ):
                                raise ValueError(
                                    "Staging database fingerprint differs; no writes allowed"
                                )
                    except BaseException:
                        await pool.close()
                        raise
                perf.instrument_pool(pool)
                _bot_pool = pool
                log.info("Pool asyncpg compartido creado exitosamente (min=1, max=%s)", pool_max)
                return pool
            except (OSError, asyncio.TimeoutError, asyncpg.PostgresConnectionError):
                if attempt == 4:
                    raise
                log.warning("postgres_connect_retry attempt=%s", attempt + 1)
                await asyncio.sleep(min(15, 2**attempt) + random.random())


def bind_bot_pool(pool):
    global _bot_pool
    _bot_pool = pool


def get_bot_pool():
    if _bot_pool is None:
        raise RuntimeError("Bot connection pool has not been initialized")
    return _bot_pool


async def close_shared_pool():
    global _bot_pool
    if _bot_pool is not None:
        try:
            if not getattr(_bot_pool, "_closed", True):
                await asyncio.wait_for(_bot_pool.close(), timeout=10)
        except Exception as e:
            log.warning("Error cerrando pool compartido: %s", e)
        finally:
            _bot_pool = None


@asynccontextmanager
async def player_connection():
    if _bot_pool is None:
        raise RuntimeError("Bot connection pool has not been initialized")
    async with _bot_pool.acquire(timeout=10) as conn:
        yield conn
