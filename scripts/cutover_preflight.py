"""Read-only GO/NO-GO for the Railway -> Wispbyte bot cutover.  python -m scripts.cutover_preflight --from-railway"""
import asyncio, json, os, ssl, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def main():
    if "--from-railway" in sys.argv:
        from scripts.perf_baseline import load_from_railway
        load_from_railway()
    import asyncpg
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    await c.execute("SET default_transaction_read_only=on")
    blockers, warns = [], []

    rooms = await c.fetch("select thread_id,room_id,status,payload from lounge_room_state where status in ('active','needs_attention') order by room_id")
    for r in rooms:
        p = json.loads(r["payload"])
        blockers.append(f"room {r['room_id']} status={r['status']} phase={p.get('phase')} started={p.get('started')} songs_posted={p.get('songs_posted')} players={len(p.get('player_ids', []))}")
    queued = 0
    for r in await c.fetch("select queue_id,payload from lounge_queue_state"):
        n = len(json.loads(r["payload"]).get("members", []))
        queued += n
    if queued:
        blockers.append(f"{queued} players waiting in queues (they would be dropped/reset)")
    pend = await c.fetchval("select count(*) from lounge_submissions where publication_status='pending'")
    if pend:
        blockers.append(f"{pend} results not yet published to Discord")
    jobs = await c.fetchval("select count(*) from lounge_role_jobs")
    if jobs:
        warns.append(f"{jobs} pending rank-role jobs (they persist and resume; drain first if possible)")
    unpub = await c.fetchval("select count(*) from lounge_room_reroll where status='selected'") if await c.fetchval("select to_regclass('public.lounge_room_reroll')") else 0
    if unpub:
        blockers.append(f"{unpub} /reroll selections saved but not published")
    lease = await c.fetchrow("select owner::text owner, expires_at, expires_at>now() live, extract(epoch from expires_at-now())::int secs from lounge_runtime_lease where id=1")
    print("lease:", dict(lease) if lease else "none (no bot connected)")
    if lease and lease["live"]:
        warns.append("Railway bot currently holds the live lease -> it must be stopped first; Wispbyte will refuse to start until the lease expires/releases")
    ver = await c.fetchval("select max(version) from lounge_schema_migrations")
    print("schema:", ver, "| reroll table:", bool(await c.fetchval("select to_regclass('public.lounge_room_reroll')")))
    season = await c.fetchrow("select id,name from seasons where active order by startdate desc limit 1")
    print("active season:", dict(season))
    print("baseline fingerprint (MMR):", await c.fetchval("select md5(string_agg(user_id||':'||season_id||':'||mmr,',' order by user_id,season_id)) from seasonplayers"))
    await c.close()
    print()
    for b in blockers:
        print("BLOCKER:", b)
    for w in warns:
        print("WARN:   ", w)
    print("\nRESULT:", "NO-GO (resolve blockers; do not stop Railway)" if blockers else "GO (data side) - still requires Wispbyte validated + rotated credentials")
    sys.exit(1 if blockers else 0)


asyncio.run(main())
