"""Read-only: after a real /reroll, show audit + room state and prove MMR/results are untouched.
   python -m scripts.post_reroll_verify --from-railway --baseline 9d3e14332158b8f39d7b9c8e73d3f219 [--thread-id N]"""
import argparse, asyncio, json, os, ssl, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--from-railway", action="store_true"); ap.add_argument("--baseline"); ap.add_argument("--thread-id", type=int); a = ap.parse_args()
    if a.from_railway:
        from scripts.perf_baseline import load_from_railway; load_from_railway()
    import asyncpg
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    await c.execute("SET default_transaction_read_only=on")
    fp = await c.fetchval("select md5(string_agg(user_id||':'||season_id||':'||mmr,',' order by user_id,season_id)) from seasonplayers")
    print("MMR fingerprint:", fp, "| equals baseline:", (fp == a.baseline) if a.baseline else "n/a (matches may legitimately change it)")
    for r in await c.fetch("select id,thread_id,room_id,admin_user_id,mode,status,published_message_id,reason,created_at from lounge_room_reroll where ($1::bigint is null or thread_id=$1) order by id desc limit 10", a.thread_id):
        print("audit:", dict(r))
    for r in await c.fetch("select thread_id,room_id,status,issue,payload from lounge_room_state where ($1::bigint is null or thread_id=$1) and status<>'finished'", a.thread_id):
        p = json.loads(r["payload"]); print(f"room {r['room_id']} {r['status']} phase={p.get('phase')} started={p.get('started')} songs_posted={p.get('songs_posted')} songs={[s[0] for s in p.get('songs') or []]} reroll_count={p.get('reroll_count')} players={p.get('player_ids')}")
    print("submissions (results) total:", await c.fetchval("select count(*) from lounge_submissions"), "| match_history rows:", await c.fetchval("select count(*) from match_history"))
    await c.close()
asyncio.run(main())
