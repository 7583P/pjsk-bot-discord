"""Read-only latency baseline for the critical read paths (no Discord needed).

Runs the *real* cog query functions against a PostgreSQL database with a
read-only session, and reports p50/p95/p99 per operation.

    python -m scripts.perf_baseline --runs 20 --label before [--out artifacts/perf-before.json]

DATABASE_URL comes from the environment, or (--from-railway) from the Railway CLI
session in memory only. Secrets are never printed or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_from_railway() -> None:
    out = subprocess.run(
        "railway variables --kv", shell=True, capture_output=True, text=True, check=True
    ).stdout
    wanted = {"DATABASE_URL", "GUILD_ID", "APPLICATION_ID"}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in wanted:
            os.environ[k] = v
    os.environ.setdefault("DB_SSL", "true")
    os.environ["DB_CA_FILE"] = str(ROOT / "certs" / "supabase_ca_bundle.crt")


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def summarize(samples: list[float]) -> dict:
    return {
        "n": len(samples),
        "p50_ms": round(pct(samples, 0.50), 1),
        "p95_ms": round(pct(samples, 0.95), 1),
        "p99_ms": round(pct(samples, 0.99), 1),
        "max_ms": round(max(samples), 1) if samples else None,
        "mean_ms": round(statistics.fmean(samples), 1) if samples else None,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out")
    ap.add_argument("--from-railway", action="store_true")
    ap.add_argument("--legacy-pool", action="store_true", help="old behaviour: reset query on every release, no cache")
    ap.add_argument("--concurrency", type=int, default=0, help="also run N parallel /top100 loads")
    args = ap.parse_args()
    if args.from_railway:
        load_from_railway()

    import asyncpg
    from lounge.config import Settings
    from lounge import perf
    import ssl as _ssl

    settings = Settings.load()
    tls = _ssl.create_default_context(cafile=settings.ca_file) if settings.ssl else False

    async def _init(conn):
        await conn.execute("SET default_transaction_read_only = on")

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=3,
        ssl=tls,
        timeout=10,
        command_timeout=30,
        statement_cache_size=0 if settings.pool_mode == "transaction" else 100,
        init=_init,
        **({} if args.legacy_pool else {"connection_class": perf.timed_connection_class(asyncpg.Connection)}),
    )
    if not args.legacy_pool:
        perf.instrument_pool(pool)

    from cogs.matchmaking import Matchmaking

    if args.legacy_pool:
        from lounge import ttlcache

        ttlcache.season_cache.ttl = 0.0  # disable caching to reproduce old behaviour
    cog = object.__new__(Matchmaking)
    cog.db_pool = pool
    cog._songs_lock = asyncio.Lock()

    results: dict[str, list[float]] = {}

    async def timed(name: str, coro_factory):
        t0 = time.perf_counter()
        try:
            await coro_factory()
        except Exception as e:  # keep going; record the failure
            results.setdefault(name + ":errors", []).append(1.0)
            print(f"  ! {name}: {type(e).__name__}: {str(e)[:120]}")
        results.setdefault(name, []).append((time.perf_counter() - t0) * 1000)

    season_id, season_name = await cog._resolve_season_id(None)
    print(f"current season = {season_id} / {season_name}")
    tiers = (1, 10, 20, 30, 40, 50, 100, 200, 300, 400, 500, 1000)

    async def rtt():
        async with pool.acquire() as c:
            await c.fetchval("SELECT 1")

    async def acquire_only():
        async with pool.acquire():
            pass

    async def top100_rows():
        rows = await cog._build_visible_leaderboard_rows(None, int(season_id))
        assert rows

    async def top100_full():
        from cogs.matchmaking import Top100View

        class _I:  # minimal interaction stand-in
            guild = None

        view = Top100View.__new__(Top100View)  # skip discord.ui.View init
        view.cog = cog
        view.db = pool
        view.inter = _I()
        view.season_id = season_id
        view.season_name = season_name
        view.page = 0
        from helpers.leaderboard import PER_PAGE, generate_top100_image

        async with pool.acquire() as c:
            rows = await cog._build_visible_leaderboard_rows(None, int(season_id), conn=c)
            await c.fetch("SELECT user_id FROM players WHERE user_id = ANY($1::bigint[])", [int(r["user_id"]) for r in rows[:100]])
        rows = [dict(r) for r in rows[:100]]
        await asyncio.to_thread(generate_top100_image, rows, season_name, 0, per_page=PER_PAGE)

    async def cutoffs():
        await cog._build_cutoffs_data(int(season_id), tiers)

    async def resolve_current():
        await cog._resolve_season_id(None)

    async def mmr_single():
        async with pool.acquire() as c:
            uid = await c.fetchval(
                "SELECT user_id FROM seasonplayers WHERE season_id=$1 ORDER BY mmr DESC LIMIT 1",
                int(season_id),
            )
        await cog._fetch_mmr_row_for_user_season(int(uid), int(season_id))

    async def mmr_all():
        async with pool.acquire() as c:
            uid = await c.fetchval(
                "SELECT user_id FROM seasonplayers WHERE season_id=$1 ORDER BY mmr DESC LIMIT 1",
                int(season_id),
            )
        await cog._fetch_mmr_rows_for_user_all_seasons(int(uid))

    async def songs_pool():
        await cog._get_random_songs(20, 30, ("expert", "master"), 5, server="en")

    ops = [
        ("rtt_select1", rtt),
        ("pool_acquire", acquire_only),
        ("resolve_current_season", resolve_current),
        ("mmr_single_season", mmr_single),
        ("mmr_all_seasons", mmr_all),
        ("cutoffs_query", cutoffs),
        ("top100_rows_query", top100_rows),
        ("top100_rows_plus_image", top100_full),
        ("songs_random_5", songs_pool),
    ]
    # warm-up (connection establishment is not part of steady state)
    await rtt()
    for name, fn in ops:
        for _ in range(args.runs):
            await timed(name, fn)
        print(f"{name:28s} {summarize(results[name])}")

    if args.concurrency:
        name = f"top100_rows_x{args.concurrency}_parallel"
        for _ in range(max(3, args.runs // 4)):
            t0 = time.perf_counter()
            await asyncio.gather(*[top100_rows() for _ in range(args.concurrency)])
            results.setdefault(name, []).append((time.perf_counter() - t0) * 1000)
        print(f"{name:28s} {summarize(results[name])}")

    await pool.close()
    report = {
        "label": args.label,
        "season": [season_id, season_name],
        "runs": args.runs,
        "ops": {k: summarize(v) for k, v in results.items() if not k.endswith(":errors")},
        "errors": {k: len(v) for k, v in results.items() if k.endswith(":errors")},
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("wrote", args.out)


if __name__ == "__main__":
    asyncio.run(main())
