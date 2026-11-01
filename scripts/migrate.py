"""Explicit migrations; never run during bot startup."""

import asyncio
import hashlib
from pathlib import Path
from dotenv import load_dotenv
import asyncpg
from lounge.config import Settings


async def migrate(settings):
    import ssl

    conn = await asyncpg.connect(
        settings.database_url,
        ssl=ssl.create_default_context(cafile=settings.ca_file)
        if settings.ssl
        else False,
        statement_cache_size=0,
    )
    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('pjsk:migrations',0))"
            )
            exists = await conn.fetchval(
                "SELECT to_regclass('public.schema_migrations')"
            )
            for path in sorted(Path("migrations").glob("*.sql")):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                previous = (
                    await conn.fetchval(
                        "SELECT checksum FROM schema_migrations WHERE version=$1",
                        path.name,
                    )
                    if exists
                    else None
                )
                if previous:
                    if previous != digest:
                        raise ValueError(
                            "La migración aplicada fue modificada: " + path.name
                        )
                    continue
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(
                    "INSERT INTO schema_migrations(version,checksum) VALUES($1,$2)",
                    path.name,
                    digest,
                )
    finally:
        await conn.close()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(migrate(Settings.load()))
