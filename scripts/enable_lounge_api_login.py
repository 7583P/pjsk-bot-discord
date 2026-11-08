"""Enable a dedicated PostgreSQL login for the FastAPI role (lounge_api). Idempotent; rotates the password.
The password exists only in memory: it is validated, then sent via stdin to Vercel's private env store."""
import argparse, asyncio, os, secrets, ssl, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
ap = argparse.ArgumentParser(); ap.add_argument("--bundle", required=True); ap.add_argument("--conn-limit", type=int, default=10)
ap.add_argument("--targets", default="preview", help="comma list of Vercel envs to store DATABASE_URL in (preview[,production])")
a = ap.parse_args()
from scripts.perf_baseline import load_from_railway
load_from_railway()
import asyncpg
ADMIN = os.environ["DATABASE_URL"]
u = urlsplit(ADMIN); ref = u.username.split(".", 1)[1]; host = u.hostname
CA = os.environ["DB_CA_FILE"]
def api_url(pw, port): return f"postgresql://lounge_api.{ref}:{pw}@{host}:{port}/postgres"

async def main():
    tls = ssl.create_default_context(cafile=CA)
    admin = await asyncpg.connect(ADMIN, ssl=tls, statement_cache_size=0)
    pw = secrets.token_urlsafe(48)
    before = await admin.fetchrow("select rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolcanlogin from pg_roles where rolname='lounge_api'")
    assert not (before["rolsuper"] or before["rolbypassrls"] or before["rolcreatedb"] or before["rolcreaterole"]), "unexpected privileged attrs"
    stmt = await admin.fetchval("select format('ALTER ROLE lounge_api WITH LOGIN CONNECTION LIMIT %s PASSWORD %L', $1::int, $2::text)", a.conn_limit, pw)
    await admin.execute(stmt)
    after = await admin.fetchrow("select rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolcanlogin,rolconnlimit from pg_roles where rolname='lounge_api'")
    print("role attrs:", dict(after))
    # ---- validate through the TRANSACTION pooler exactly as Vercel will connect
    ok = True
    def rep(name, cond, d=""):
        nonlocal ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name + (f" [{d}]" if d else ""))
    for attempt in range(6):
        try:
            c = await asyncpg.connect(api_url(pw, 6543), ssl=tls, statement_cache_size=0, timeout=20); break
        except Exception as e:
            print("pooler not ready yet:", type(e).__name__); await asyncio.sleep(5)
    else:
        raise SystemExit("could not connect as lounge_api")
    rep("current_user is lounge_api", await c.fetchval("select current_user") == "lounge_api")
    rep("not superuser / no bypassrls", not await c.fetchval("select rolsuper or rolbypassrls from pg_roles where rolname=current_user"))
    rep("RLS read works (seasonplayers)", await c.fetchval("select count(*) from seasonplayers") > 0)
    rep("reads songs/players/results", all([await c.fetchval(f"select count(*) from {t}") >= 0 for t in ("songs", "players", "results", "match_history", "player_titles", "player_pfps")]))
    async def denied(sql, *args):
        tr = c.transaction(); await tr.start()
        try:
            await c.execute(sql, *args); return False
        except asyncpg.InsufficientPrivilegeError: return True
        finally: await tr.rollback()
    rep("cannot write seasonplayers (MMR)", await denied("update seasonplayers set mmr=mmr where false or user_id=-1") or await denied("insert into seasonplayers(user_id,season_id,mmr) values(-1,-1,0)"))
    rep("cannot update players.name", await denied("update players set name=name where user_id=-1"))
    rep("cannot touch lounge_room_state", await denied("update lounge_room_state set status=status where thread_id=-1"))
    rep("cannot touch lounge_room_reroll", await denied("insert into lounge_room_reroll(operation_id,thread_id,room_id,season_id,admin_user_id,reason,mode,new_selection,status) values('x',-1,1,1,1,'x','auto','[]','selected')"))
    # permitted writes, always rolled back
    tr = c.transaction(); await tr.start()
    try:
        await c.execute("insert into lounge_oauth_states(state_hash,expires_at) values('verify-'||md5(random()::text), now()+interval '1 minute')")
        await c.execute("update players set selected_title_key=selected_title_key where user_id=-1")
        await c.execute("update players set discord_id=discord_id where user_id=-1")
        rep("profile/session writes permitted (rolled back)", True)
    except Exception as e:
        rep("profile/session writes permitted (rolled back)", False, type(e).__name__)
    finally: await tr.rollback()
    await c.close()
    if not ok:
        await admin.execute("ALTER ROLE lounge_api NOLOGIN"); raise SystemExit("validation failed; login disabled again")
    for env in a.targets.split(","):
        r = subprocess.run(["npx.cmd", "--yes", "vercel@latest", "env", "add", "DATABASE_URL", env, "--force", "--yes", "--sensitive"], input=api_url(pw, 6543), text=True, capture_output=True, cwd=a.bundle)
        print(f"vercel env DATABASE_URL[{env}] stored rc={r.returncode}")
    await admin.close()
asyncio.run(main())
