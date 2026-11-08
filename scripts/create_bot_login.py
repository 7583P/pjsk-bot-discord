"""Enable a dedicated login for the definitive bot (role lounge_bot) instead of 'postgres'. Existing grants/policies are NOT changed.

  python -m scripts.create_bot_login --from-railway             # enable/rotate password, validate, write private file
  python -m scripts.create_bot_login --from-railway --disable    # NOLOGIN again (rollback)

Validation proves (without changing data) that every table the bot code writes is writable by this role (zero-row
INSERT/UPDATE/DELETE inside a rolled-back transaction), that DDL / superuser actions are denied, and that it connects
through the Supabase session pooler with verified TLS. The DSN goes to a user-only file; nothing sensitive is printed.
"""
import argparse, asyncio, os, secrets, ssl, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ROLE = "lounge_bot"
OUT = Path.home() / ".pjsk_secure_backups" / "wispbyte_bot.env"
# tables main.py + loaded cogs write to (from a static scan of the code) and the operations used
WRITES = {
    "banned_songs": "IU", "global_settings": "IU", "lounge_queue_state": "IUD", "lounge_role_jobs": "IUD",
    "lounge_room_members": "IUD", "lounge_room_reroll": "IU", "lounge_room_state": "IUD", "lounge_runtime_lease": "IUD",
    "lounge_submissions": "IU", "match_history": "IU", "match_songs": "IU", "player_title_inventory": "IUD",
    "player_titles": "IUD", "players": "IUD", "results": "IU", "seasonplayers": "IUD", "seasons": "IU", "songs": "IUD",
}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-railway", action="store_true")
    ap.add_argument("--disable", action="store_true")
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

    if a.disable:
        await admin.execute(f"ALTER ROLE {ROLE} NOLOGIN")
        OUT.unlink(missing_ok=True)
        print("lounge_bot login disabled; private file removed")
        await admin.close()
        return

    before = await admin.fetchrow("select rolsuper,rolbypassrls,rolcreatedb,rolcreaterole from pg_roles where rolname=$1", ROLE)
    assert not any(before.values()), "lounge_bot has privileged attributes; refusing"
    pw = secrets.token_urlsafe(40)
    await admin.execute(await admin.fetchval("select format('ALTER ROLE %I WITH LOGIN CONNECTION LIMIT 8 PASSWORD %L', $1::text, $2::text)", ROLE, pw))
    dsn = f"postgresql://{ROLE}.{ref}:{pw}@{u.hostname}:5432/postgres"
    ok = True

    def rep(name, cond, d=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f" [{d}]" if d else ""))

    for _ in range(6):
        try:
            c = await asyncpg.connect(dsn, ssl=tls, timeout=20)
            break
        except Exception as e:
            print("pooler not ready:", type(e).__name__)
            await asyncio.sleep(4)
    else:
        await admin.execute(f"ALTER ROLE {ROLE} NOLOGIN")
        raise SystemExit("could not log in as lounge_bot; login disabled again")

    rep("connects as lounge_bot (session pooler, verified TLS)", await c.fetchval("select current_user") == ROLE)
    rep("not superuser / bypassrls / createrole / createdb", not await c.fetchval("select rolsuper or rolbypassrls or rolcreaterole or rolcreatedb from pg_roles where rolname=current_user"))
    rep("statement_timeout configured", bool(await c.fetchval("show statement_timeout")), await c.fetchval("show statement_timeout"))
    rep("reads active season", bool(await c.fetchval("select name from seasons where active order by startdate desc limit 1")))
    rep("reads songs catalog", await c.fetchval("select count(*) from songs") > 1000)

    # zero-row writes: exercise privilege + RLS for each table, then roll everything back
    failed = []
    tr = c.transaction()
    await tr.start()
    try:
        for t, ops in WRITES.items():
            stmts = []
            if "I" in ops:
                stmts.append(f"insert into public.{t} select * from public.{t} where false")
            if "U" in ops:
                cols = await c.fetchval("select column_name from information_schema.columns where table_schema='public' and table_name=$1 order by ordinal_position limit 1", t)
                stmts.append(f'update public.{t} set "{cols}"="{cols}" where false')
            if "D" in ops:
                stmts.append(f"delete from public.{t} where false")
            for s in stmts:
                sp = c.transaction()
                await sp.start()
                try:
                    await c.execute(s)
                    await sp.commit()
                except Exception as e:
                    await sp.rollback()
                    failed.append((t, s.split()[0], type(e).__name__))
    finally:
        await tr.rollback()
    rep(f"zero-row INSERT/UPDATE/DELETE allowed on all {len(WRITES)} bot tables", not failed, str(failed[:3]))

    async def denied(sql):
        try:
            await c.execute(sql)
            return False
        except (asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError):
            return True

    rep("DDL denied (create table)", await denied("create table public._bot_probe_x(a int)"))
    rep("cannot alter roles", await denied("alter role postgres password 'x'"))
    rep("cannot truncate players", await denied("truncate public.players"))
    rep("cannot touch API-only OAuth state writes? (insert denied)", await denied("insert into public.lounge_oauth_states select * from public.lounge_oauth_states where false"))
    # lease semantics work for this role (read-only check, no claim)
    rep("lease table readable", await c.fetchval("select count(*) from lounge_runtime_lease") >= 0)
    await c.close()
    await admin.close()
    if not ok:
        a2 = await asyncpg.connect(admin_url, ssl=tls, statement_cache_size=0)
        await a2.execute(f"ALTER ROLE {ROLE} NOLOGIN")
        await a2.close()
        raise SystemExit("validation failed; login disabled again; DSN NOT handed out")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(f'DATABASE_URL="{dsn}"\n', encoding="utf-8")
    if os.name == "nt":
        subprocess.run(["icacls", str(OUT), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"], capture_output=True)
    print(f"\nbot DSN ready ({len(dsn)} chars, password hidden)\nprivate file: {OUT}\n(no clipboard copy: add DISCORD_TOKEN yourself at cutover)")


asyncio.run(main())
