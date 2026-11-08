"""Enable a dedicated PostgreSQL login for the FastAPI role (lounge_api). Idempotent; rotates the password.
The password exists only in memory: it is validated, then sent via stdin to Vercel's private env store."""
import argparse, asyncio, os, secrets, ssl, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
ap = argparse.ArgumentParser(); ap.add_argument("--bundle", required=True); ap.add_argument("--conn-limit", type=int, default=10)
ap.add_argument("--smoke-bundle", help="after validation, run the API bundle locally with these credentials and smoke-test it")
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
    if a.smoke_bundle:
        await smoke(Path(a.smoke_bundle).resolve(), api_url(pw, 6543), admin)
    await admin.close()


async def smoke(bundle, url, admin):
    import httpx, time, statistics, secrets as sec
    port = 8766; base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, DATABASE_URL=url, DB_POOL_MODE="transaction", DB_POOL_MAX="1", DB_SSL="true", DB_CA_FILE=str(bundle / "certs" / "supabase_ca_bundle.crt"),
               SESSION_SECRET=sec.token_urlsafe(48), VERCEL="1", FRONT_URL=base, CORS_ORIGIN=base, DISCORD_REDIRECT_URI=base + "/auth02/callback", DISCORD_CLIENT_ID="1371546746248761455", COOKIE_NAME="pjsk_session")
    for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_SERVICE_NAME"): env.pop(k, None)
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], cwd=bundle, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    results = []
    def rep(n, ok, d=""):
        results.append(ok); print(("PASS " if ok else "FAIL ") + n + (f" [{d}]" if d else ""))
    try:
        async with httpx.AsyncClient(base_url=base, timeout=40, follow_redirects=False) as c:
            for _ in range(40):
                try: await c.get("/health"); break
                except Exception: await asyncio.sleep(1)
            for p in ["/health", "/health/ready", "/api/seasons", "/api/players?limit=5", "/api/hall-of-fame", "/api/season-cutoffs", "/api/matches?limit=3", "/api/runtime-info", "/"]:
                ts = []
                for _ in range(3):
                    t = time.perf_counter(); r = await c.get(p); ts.append((time.perf_counter() - t) * 1000)
                rep(f"GET {p}", r.status_code == 200, f"{r.status_code} first={ts[0]:.0f}ms warm={statistics.median(ts[1:]):.0f}ms")
            rep("seasons contain active Season 5", "Season 5" in (await c.get("/api/seasons")).text)
            rep("debug routes closed", (await c.get("/api/debug-version")).status_code == 404)
            r = await c.get("/auth02"); loc = r.headers.get("location", ""); sc = r.headers.get("set-cookie", "").lower()
            rep("/auth02 -> Discord authorize with configured redirect_uri (write via lounge_api: oauth state)", r.status_code in (302, 307) and "discord.com/oauth2/authorize" in loc and "redirect_uri=http%3A%2F%2F127.0.0.1%3A8766%2Fauth02%2Fcallback" in loc, str(r.status_code))
            rep("state cookie Secure+HttpOnly+SameSite=Lax", all(x in sc for x in ("secure", "httponly", "samesite=lax")))
            r = await c.get("/auth02/callback", params={"code": "x", "state": "forged"}); rep("callback rejects forged state without 5xx", r.status_code < 500, str(r.status_code))
            rep("/auth02/me without session -> 401", (await c.get("/auth02/me")).status_code == 401)
            r = await c.get("/api/players?limit=1", headers={"origin": base}); rep("CORS allows front origin", r.headers.get("access-control-allow-origin") == base)
            r = await c.get("/api/players?limit=1", headers={"origin": "https://evil.example"}); rep("CORS denies foreign origin", "access-control-allow-origin" not in r.headers)
            r = await c.post("/auth02/logout", headers={"origin": "https://evil.example"}); rep("cross-origin write rejected", r.status_code in (400, 401, 403), str(r.status_code))
            peak = 0
            async def watch():
                nonlocal peak
                while True:
                    peak = max(peak, await admin.fetchval("select count(*) from pg_stat_activity where usename='lounge_api'")); await asyncio.sleep(0.2)
            w = asyncio.create_task(watch()); t = time.perf_counter()
            rs = await asyncio.gather(*[c.get(p) for p in ["/api/seasons", "/api/players?limit=5", "/api/hall-of-fame"] * 10]); w.cancel()
            rep("30 concurrent requests all 200", all(x.status_code == 200 for x in rs), f"{(time.perf_counter()-t)*1000:.0f}ms")
            rep("lounge_api connections stay <= its limit (10)", peak <= 10, f"peak={peak}")
        grep = subprocess.run(["grep", "-rIil", "railway", str(bundle / "app.py"), str(bundle / "lounge"), str(bundle / "vercel.json"), str(bundle / "requirements.txt")], capture_output=True, text=True).stdout.split()
        rep("no 'railway' reference in API bundle", not grep, ",".join(Path(g).name for g in grep))
    finally:
        srv.terminate()
        try: srv.wait(10)
        except Exception: srv.kill()
    print(f"SMOKE: {sum(results)}/{len(results)} passed")

asyncio.run(main())
