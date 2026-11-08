"""Authorized maintenance reset of OPEN rooms/queues (no MMR, results, players, seasons or profiles are touched).

  python -m scripts.maintenance_room_reset --from-railway            # dry-run: plan only (reads DB + Discord REST GETs)
  python -m scripts.maintenance_room_reset --from-railway --execute  # requires the bot writer to be stopped (lease expired)

Effects (execute): notify + archive/lock each open room thread (threads are NOT deleted), remove queue roles from its
players, mark lounge_room_state rows 'closed' (history kept), delete their lounge_room_members reservations, empty the
queue snapshots, and record the operation in lounge_operations. All DB changes are one transaction.
"""
import argparse, asyncio, datetime, json, os, ssl, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
API = "https://discord.com/api/v10"
QUEUE_ROLES = {"en": 1451255910469996726, "jp": 1451259854693072927}
NOTICE = ("⚠️ **This room was cancelled for maintenance.** No MMR, rank or competitive penalty applies and no result was recorded. "
          "Please join the queue again with /c when matchmaking reopens. / "
          "メンテナンスのためこのルームはキャンセルされました。MMRやランクへのペナルティはありません。再度 /c で参加してください。")


def load_env():
    out = subprocess.run("railway variables --kv", shell=True, capture_output=True, text=True, check=True).stdout
    rv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    os.environ["DATABASE_URL"] = rv["DATABASE_URL"]
    os.environ["DB_CA_FILE"] = str(ROOT / "certs" / "supabase_ca_bundle.crt")
    return rv["DISCORD_TOKEN"], int(rv["GUILD_ID"])


async def discord(client, method, path, token, **kw):
    for _ in range(5):
        r = await client.request(method, API + path, headers={"Authorization": f"Bot {token}", "X-Audit-Log-Reason": "maintenance reset"}, **kw)
        if r.status_code == 429:
            await asyncio.sleep(float(r.json().get("retry_after", 1)) + 0.2)
            continue
        return r
    return r


async def fingerprint(c):
    return {
        "mmr": await c.fetchval("select md5(string_agg(user_id||':'||season_id||':'||mmr,',' order by user_id,season_id)) from seasonplayers"),
        "seasonplayers": await c.fetchval("select count(*) from seasonplayers"),
        "match_history": await c.fetchval("select count(*) from match_history"),
        "results": await c.fetchval("select count(*) from results"),
        "submissions": await c.fetchval("select count(*) from lounge_submissions"),
        "players": await c.fetchval("select count(*) from players"),
        "titles": await c.fetchval("select md5(string_agg(coalesce(selected_title_key,''),',' order by user_id)) from players"),
        "active_season": await c.fetchval("select id from seasons where active order by startdate desc limit 1"),
    }


async def _main(a):
    ap = None
    token, guild_id = load_env()
    import asyncpg, httpx
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    if not a.execute:
        await c.execute("SET default_transaction_read_only=on")
    rooms = await c.fetch("select thread_id,room_id,queue_id,status,payload from lounge_room_state where status in ('active','needs_attention') order by room_id, thread_id")
    plan = []
    for r in rooms:
        p = json.loads(r["payload"])
        sub = await c.fetchval("select result_id from lounge_submissions where thread_id=$1", r["thread_id"])
        plan.append(dict(thread_id=r["thread_id"], room_id=r["room_id"], queue_id=r["queue_id"], status=r["status"], phase=p.get("phase"), started=p.get("started"),
                         server=p.get("server", "en"), players=[int(x) for x in p.get("player_ids", [])], has_result=sub))
    queues = await c.fetch("select queue_id,payload from lounge_queue_state")
    qmembers = {q["queue_id"]: json.loads(q["payload"]).get("members", []) for q in queues}
    members_rows = await c.fetchval("select count(*) from lounge_room_members")
    lease = await c.fetchrow("select expires_at>now() live, extract(epoch from expires_at-now())::int secs from lounge_runtime_lease where id=1")
    before = await fingerprint(c)
    print("open rooms:", len(plan))
    async with httpx.AsyncClient(timeout=20) as hc:
        for p in plan:
            r = await discord(hc, "GET", f"/channels/{p['thread_id']}", token)
            p["discord"] = (r.status_code, (r.json().get("thread_metadata") or {}).get("archived") if r.status_code == 200 else None)
            p["queue_roles"] = {}
            for uid in p["players"]:
                m = await discord(hc, "GET", f"/guilds/{guild_id}/members/{uid}", token)
                p["queue_roles"][uid] = (m.status_code == 200 and any(int(x) in QUEUE_ROLES.values() for x in m.json().get("roles", [])))
            print(" -", {k: v for k, v in p.items() if k != "queue_roles"}, "| players holding queue role:", sum(p["queue_roles"].values()))
    print("queue snapshot members:", {k: len(v) for k, v in qmembers.items()}, "| reserved member rows:", members_rows)
    print("lease:", dict(lease) if lease else "none")
    print("baseline:", before)
    if any(p["has_result"] for p in plan):
        raise SystemExit("ABORT: a room already has a recorded result; resolve manually")
    if not a.execute:
        print("\ndry-run: nothing changed")
        return
    if lease and lease["live"]:
        raise SystemExit("ABORT: the bot still holds a live lease; stop the writer first (races would resurrect rooms)")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = ROOT / "backups" / f"room-reset-snapshot-{stamp}.json"
    snap.write_text(json.dumps({"rooms": [dict(r, payload=json.loads(r["payload"])) for r in map(dict, rooms)], "queues": [dict(q, payload=json.loads(q["payload"])) for q in map(dict, queues)],
                                "members": [dict(r) for r in await c.fetch("select * from lounge_room_members")], "baseline": before}, default=str, indent=1))
    print("snapshot:", snap.name)

    async with httpx.AsyncClient(timeout=20) as hc:
        for p in plan:
            if p["discord"][0] != 200 and p["players"]:
                mentions = " ".join(f"<@{u}>" for u in p["players"])
                r = await discord(hc, "POST", f"/channels/{p['queue_id']}/messages", token, json={"content": mentions + "\n" + NOTICE, "allowed_mentions": {"users": [str(u) for u in p["players"]]}})
                print(" fallback notice in queue channel", r.status_code)
            if p["discord"][0] == 200:
                r = await discord(hc, "POST", f"/channels/{p['thread_id']}/messages", token, json={"content": NOTICE, "allowed_mentions": {"parse": []}})
                print(" notice", p["thread_id"], r.status_code)
                r = await discord(hc, "PATCH", f"/channels/{p['thread_id']}", token, json={"archived": True, "locked": True})
                print(" archive+lock", p["thread_id"], r.status_code)
            for uid, has in p["queue_roles"].items():
                if has:
                    for rid in QUEUE_ROLES.values():
                        await discord(hc, "DELETE", f"/guilds/{guild_id}/members/{uid}/roles/{rid}", token)

    async with c.transaction():
        await c.execute("SET LOCAL lock_timeout='5s'")
        threads = [p["thread_id"] for p in plan]
        await c.execute("""update lounge_room_state set status='closed', issue='maintenance_reset', updated_at=now(),
                           payload = payload || '{"closed":true,"finished":true,"cancelled_reason":"maintenance_reset"}'::jsonb
                           where thread_id = any($1::bigint[]) and status in ('active','needs_attention')""", threads)
        await c.execute("delete from lounge_room_members where thread_id = any($1::bigint[])", threads)
        await c.execute("""update lounge_queue_state set payload='{"members":[],"order":{}}'::jsonb, updated_at=now()""")
        await c.execute("insert into lounge_operations(id,kind,payload) values($1,'maintenance_room_reset',$2::jsonb)", f"maintenance_room_reset:{stamp}",
                        json.dumps({"threads": threads, "rooms": [p["room_id"] for p in plan], "players": sorted({u for p in plan for u in p["players"]}), "reason": "authorized by owner; no competitive penalty"}))
    after = await fingerprint(c)
    left = dict(
        open_rooms=await c.fetchval("select count(*) from lounge_room_state where status in ('active','needs_attention')"),
        reserved_members=await c.fetchval("select count(*) from lounge_room_members"),
        queued=sum(len(json.loads(q["payload"]).get("members", [])) for q in await c.fetch("select payload from lounge_queue_state")),
        unpublished_results=await c.fetchval("select count(*) from lounge_submissions where publication_status='pending'"),
    )
    print("remaining:", left)
    print("competitive data unchanged:", before == after)
    if before != after:
        print("DIFF:", {k: (before[k], after[k]) for k in before if before[k] != after[k]})
    await c.close()


def rv_set(value):
    r = subprocess.run(f'railway variables --set "DISCORD_TOKEN={value}"', shell=True, capture_output=True, text=True)
    return r.returncode


async def lease_live():
    import asyncpg
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    try:
        return bool(await c.fetchval("select coalesce((select expires_at>now() from lounge_runtime_lease where id=1), false)"))
    finally:
        await c.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-railway", action="store_true")
    ap.add_argument("--execute", action="store_true", help="bot writer must already be stopped")
    ap.add_argument("--orchestrate", action="store_true", help="stop the Railway bot writer (DISABLED+token), reset, then ALWAYS restore")
    a = ap.parse_args()
    if not a.orchestrate:
        return await _main(a)
    token, _ = load_env()
    print("stopping bot writer (API stays up)...")
    assert rv_set("DISABLED" + token) == 0, "could not set variable"
    try:
        for i in range(60):
            await asyncio.sleep(5)
            if not await lease_live():
                print(f"lease released after ~{(i + 1) * 5}s; bot writer is down")
                break
        else:
            raise SystemExit("lease still live after 300s; aborting (variable will be restored)")
        a.execute = True
        await _main(a)
    finally:
        rc = rv_set(token)
        print("DISCORD_TOKEN restored, rc =", rc, "(Railway redeploys the bot)")


asyncio.run(main())
