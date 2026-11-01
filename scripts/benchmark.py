"""Compare the original per-member read pattern against one aggregate, same DB.

Run only against an already seeded local test/staging DB. No production mutation.
"""

import argparse
import asyncio
import json
import statistics
import time
from urllib.parse import urlsplit
from dotenv import load_dotenv
import httpx
from lounge.config import Settings
from lounge.database import Database
from app import create_app


def summary(samples):
    values = sorted(samples)
    return dict(
        p50_ms=round(statistics.median(values), 3),
        p95_ms=round(values[int(0.95 * (len(values) - 1))], 3),
        samples=len(values),
    )


async def benchmark(db):
    if urlsplit(db.settings.database_url).hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        raise ValueError("Benchmark solo en PostgreSQL local")
    async with db.pool.acquire() as c:
        ids = await c.fetch(
            "SELECT sp.user_id FROM season_players sp JOIN seasons s ON s.id=sp.season_id WHERE s.status='active' ORDER BY user_id LIMIT 50"
        )
        season = await c.fetchval("SELECT id FROM seasons WHERE status='active'")
        old = []
        new = []
        for _ in range(50):
            start = time.perf_counter()
            for p in ids:
                await c.fetchval(
                    "SELECT mmr FROM season_players WHERE season_id=$1 AND user_id=$2",
                    season,
                    p["user_id"],
                )
            old.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            await c.fetch(
                "SELECT user_id,mmr FROM season_players WHERE season_id=$1 AND user_id=ANY($2::bigint[])",
                season,
                [p["user_id"] for p in ids],
            )
            new.append((time.perf_counter() - start) * 1000)
        plan = await c.fetch(
            "EXPLAIN (ANALYZE,FORMAT JSON) SELECT user_id,mmr FROM season_players WHERE season_id=$1 ORDER BY mmr DESC,user_id LIMIT 10",
            season,
        )
    app = create_app(db)
    api = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for _ in range(50):
            start = time.perf_counter()
            r = await client.get("/api/players")
            r.raise_for_status()
            api.append((time.perf_counter() - start) * 1000)
    return dict(
        scope="local loopback; no TLS, no Discord or Supabase WAN; no throughput/24h guarantee",
        players=len(ids),
        legacy_member_reads=summary(old),
        batched_reads=summary(new),
        legacy_queries=len(ids),
        batched_queries=1,
        api=summary(api),
        query_plan=json.loads(plan[0][0]),
    )


async def main(args):
    db = Database(Settings.load())
    await db.open()
    try:
        report = await benchmark(db)
        from pathlib import Path

        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps({k: v for k, v in report.items() if k != "query_plan"}, indent=2)
        )
    finally:
        await db.close()


if __name__ == "__main__":
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="artifacts/benchmark.json")
    asyncio.run(main(p.parse_args()))
