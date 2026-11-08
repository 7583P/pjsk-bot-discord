"""Create/refresh a least-privilege, READ-ONLY login for host probes (lounge_probe) and hand its DSN to the owner safely.

  python -m scripts.create_probe_login --from-railway            # create/rotate, validate, write private file + clipboard
  python -m scripts.create_probe_login --from-railway --revoke    # NOLOGIN + remove grants/policies (cleanup after the probe)

The password exists only in memory, in a user-only file under ~/.pjsk_secure_backups and (optionally) the clipboard.
Nothing sensitive is printed.  It touches no existing role (lounge_bot/lounge_api/postgres) and no data.
"""
import argparse, asyncio, os, secrets, ssl, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ROLE = "lounge_probe"
TABLES = ("seasons", "seasonplayers", "lounge_room_state")  # exactly what probe.py reads
OUT = Path.home() / ".pjsk_secure_backups" / "wispbyte_probe.env"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-railway", action="store_true")
    ap.add_argument("--revoke", action="store_true")
    ap.add_argument("--no-clipboard", action="store_true")
    a = ap.parse_args()
    if a.from_railway:
        from scripts.perf_baseline import load_from_railway
        load_from_railway()
    import asyncpg
    admin_url = os.environ["DATABASE_URL"]
    u = urlsplit(admin_url)
    ref = u.username.split(".", 1)[1]
    tls = ssl.create_default_context(cafile=os.environ["DB_CA_FILE"])
    admin = await asyncpg.connect(admin_url, ssl=tls, statement_cache_size=0)
    exists = await admin.fetchval("select exists(select 1 from pg_roles where rolname=$1)", ROLE)

    if a.revoke:
        if exists:
            for t in TABLES:
                await admin.execute(f'DROP POLICY IF EXISTS probe_read ON public."{t}"')
                await admin.execute(f'REVOKE ALL ON public."{t}" FROM {ROLE}')
            await admin.execute(f"ALTER ROLE {ROLE} NOLOGIN")
        print("probe login disabled" if exists else "nothing to revoke")
        OUT.unlink(missing_ok=True)
        await admin.close()
        return

    pw = secrets.token_urlsafe(40)  # [-_A-Za-z0-9] only: safe inside a URL without escaping
    if not exists:
        await admin.execute(f"CREATE ROLE {ROLE} LOGIN NOINHERIT CONNECTION LIMIT 2")
    stmt = await admin.fetchval("select format('ALTER ROLE %I WITH LOGIN CONNECTION LIMIT 2 PASSWORD %L', $1::text, $2::text)", ROLE, pw)
    await admin.execute(stmt)
    await admin.execute(f"ALTER ROLE {ROLE} SET statement_timeout='10s'")
    await admin.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only=on")
    await admin.execute(f"ALTER ROLE {ROLE} SET idle_in_transaction_session_timeout='15s'")
    async with admin.transaction():
        for t in TABLES:
            await admin.execute(f'GRANT SELECT ON public."{t}" TO {ROLE}')
            await admin.execute(f'DROP POLICY IF EXISTS probe_read ON public."{t}"')
            await admin.execute(f'CREATE POLICY probe_read ON public."{t}" FOR SELECT TO {ROLE} USING (true)')

    dsn = f"postgresql://{ROLE}.{ref}:{pw}@{u.hostname}:5432/postgres"  # session pooler, same host as the bot
    # ---------- validation, exactly as the host will connect (session pooler, verified TLS)
    p = urlsplit(dsn)
    assert p.scheme == "postgresql" and p.hostname == u.hostname and p.port == 5432 and p.path == "/postgres" and p.username == f"{ROLE}.{ref}"
    ok = True
    for _ in range(6):
        try:
            c = await asyncpg.connect(dsn, ssl=tls, timeout=20)
            break
        except Exception as e:
            print("pooler not ready:", type(e).__name__)
            await asyncio.sleep(4)
    else:
        raise SystemExit("could not log in as the probe role")

    def rep(name, cond, d=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f" [{d}]" if d else ""))

    rep("connects as lounge_probe over verified TLS", await c.fetchval("select current_user") == ROLE)
    rep("not superuser / no bypassrls / not createrole", not await c.fetchval("select rolsuper or rolbypassrls or rolcreaterole or rolcreatedb from pg_roles where rolname=current_user"))
    rep("session is read-only by default", await c.fetchval("show default_transaction_read_only") == "on")
    rep("reads active season", bool(await c.fetchval("select name from seasons where active order by startdate desc limit 1")))
    rep("reads top-100 query (RLS policy works)", len(await c.fetch("select user_id,mmr from seasonplayers where season_id=(select id from seasons where active order by startdate desc limit 1) order by mmr desc limit 100")) > 0)
    rep("reads room count", await c.fetchval("select count(*) from lounge_room_state") is not None)

    async def denied(sql):
        try:
            await c.fetch(sql)
            return False
        except (asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError):
            return True

    for t in ("players", "match_history", "results", "lounge_submissions", "lounge_oauth_states", "player_pfps"):
        rep(f"cannot read {t}", await denied(f"select * from {t} limit 1"))
    rep("cannot write seasonplayers", await denied("update seasonplayers set mmr=mmr where user_id=-1"))
    await c.close()
    await admin.close()
    if not ok:
        raise SystemExit("validation failed; NOT handing out the DSN")

    # ---------- hand-off: user-only file + clipboard (never printed)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f'DATABASE_URL="{dsn}"\n', encoding="utf-8")
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(OUT), "/inheritance:r", "/grant:r", f"{user}:F"], capture_output=True)
        if not a.no_clipboard:
            subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"], input=f'DATABASE_URL="{dsn}"', text=True, capture_output=True)
    print(f"\nDSN ready ({len(dsn)} chars, password hidden)")
    print(f"private file : {OUT}")
    print("clipboard    : full .env line copied" if (os.name == 'nt' and not a.no_clipboard) else "clipboard    : skipped")


asyncio.run(main())
