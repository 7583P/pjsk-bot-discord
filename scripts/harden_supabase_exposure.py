"""Close anon/authenticated access to non-public tables. Reversible: no table or row is dropped or changed.

phases: snapshot ACL+RLS state -> pg_dump of the affected tables -> apply (one transaction) -> verify via SET ROLE
        -> automatic rollback from the snapshot if anything fails.   `--rollback FILE` restores a snapshot.
"""
import argparse, asyncio, datetime, json, os, ssl, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PRIV = ["anon", "authenticated"]
NO_RLS = ["backup_ferm_match_history_20260725_2226", "backup_ferm_players_20260725_2226", "backup_ferm_seasonplayers_20260725_2226",
          "match_history_backup_recalc", "placements", "cards_cache", "card_variants_cache", "sekaicards_meta"]
RLS_ONLY = ["lounge_queue_state", "lounge_room_members", "lounge_room_reroll"]
ALL_T = NO_RLS + RLS_ONLY


def q(n):
    return '"' + n.replace('"', '""') + '"'


APPLY = ["SET LOCAL lock_timeout='3s'"]
for t in NO_RLS:
    APPLY += [f"ALTER TABLE public.{q(t)} ENABLE ROW LEVEL SECURITY",
              f"REVOKE ALL ON TABLE public.{q(t)} FROM anon, authenticated, PUBLIC"]
for t in RLS_ONLY:
    APPLY += [f"REVOKE ALL ON TABLE public.{q(t)} FROM anon, authenticated"]
APPLY += ["REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated",
          "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated",
          "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated",
          "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated"]

PERM = {"r": "SELECT", "w": "UPDATE", "a": "INSERT", "d": "DELETE", "D": "TRUNCATE", "x": "REFERENCES", "t": "TRIGGER", "U": "USAGE", "m": "MAINTAIN"}


async def connect():
    import asyncpg
    return await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)


async def snapshot(c):
    snap = {"taken": datetime.datetime.now(datetime.timezone.utc).isoformat(), "tables": {}, "sequences": {}, "default_acl": []}
    rows = await c.fetch("select c.relname, c.relkind::text relkind, c.relrowsecurity rls, c.relacl::text acl from pg_class c "
                         "join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind in('r','S')")
    for r in rows:
        (snap["tables"] if r["relkind"] == "r" else snap["sequences"])[r["relname"]] = {"rls": r["rls"], "acl": r["acl"]}
    for r in await c.fetch("select defaclobjtype::text t, defaclacl::text acl from pg_default_acl "
                           "where pg_get_userbyid(defaclrole)='postgres' and defaclnamespace::regnamespace::text='public'"):
        snap["default_acl"].append({"type": r["t"], "acl": r["acl"]})
    return snap


def rollback_sql(snap):
    """Re-grant to anon/authenticated/PUBLIC exactly what the snapshot recorded, plus RLS flags."""
    out = []

    def grants(kind, name, acl):
        for entry in (acl or "").strip("{}").split(","):
            if "=" not in entry or not entry:
                continue
            who, rest = entry.split("=", 1)
            if who not in ("anon", "authenticated", ""):
                continue
            ps = ", ".join(PERM[p] for p in rest.split("/")[0] if p in PERM)
            if ps:
                out.append(f"GRANT {ps} ON {kind} public.{q(name)} TO {'PUBLIC' if who == '' else who}")

    for t, v in snap["tables"].items():
        if t in ALL_T:
            grants("TABLE", t, v["acl"])
            if t in NO_RLS and not v["rls"]:
                out.append(f"ALTER TABLE public.{q(t)} DISABLE ROW LEVEL SECURITY")
    for s, v in snap["sequences"].items():
        grants("SEQUENCE", s, v["acl"])
    for d in snap["default_acl"]:
        kind = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS"}.get(d["type"])
        if kind and any(x.startswith(("anon=", "authenticated=")) for x in d["acl"].strip("{}").split(",")):
            out.append(f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON {kind} TO anon, authenticated")
    return out


async def run_sql(c, stmts):
    async with c.transaction():
        for s in stmts:
            await c.execute(s)


BEFORE = {}


async def runtime_fingerprint(c):
    out = {}
    for role in ("lounge_api", "lounge_bot"):
        grants = [tuple(r) for r in await c.fetch(
            "select table_name,privilege_type from information_schema.role_table_grants where grantee=$1 and table_schema='public' "
            "union all select table_name||'.'||column_name,privilege_type from information_schema.column_privileges where grantee=$1 and table_schema='public' and privilege_type<>'SELECT' "
            "order by 1,2", role)]
        grants += [("seq:" + r[0], r[1]) for r in await c.fetch(
            "select c.relname, p from pg_class c join pg_namespace n on n.oid=c.relnamespace, unnest(array['USAGE','SELECT','UPDATE']) p "
            "where n.nspname='public' and c.relkind='S' and has_sequence_privilege($1,c.oid,p) order by 1,2", role)]
        pol = [tuple(r) for r in await c.fetch("select tablename,policyname,cmd from pg_policies where $1=any(roles::text[]) order by 1,2", role)]
        out[role] = {"grants": grants, "policies": pol}
    return out


async def verify(c):
    ok = True

    def rep(n, cond, d=""):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + n + (f" [{d}]" if d else ""))

    async def as_role(role, sql):
        tr = c.transaction()
        await tr.start()
        try:
            await c.execute(f"SET LOCAL ROLE {role}")
            r = await c.fetch(sql)
            return ("ok", len(r))
        except Exception as e:
            return (type(e).__name__, 0)
        finally:
            await tr.rollback()

    for role in PRIV:
        for t in ALL_T:
            res = await as_role(role, f"select * from public.{q(t)} limit 1")
            rep(f"{role} cannot read {t}", res[0] == "InsufficientPrivilegeError", res[0])
        for t in ("players", "seasonplayers", "match_history", "results", "songs"):
            res = await as_role(role, f"select * from public.{q(t)} limit 1")
            rep(f"{role} sees no rows of {t}", res[0] == "InsufficientPrivilegeError" or res == ("ok", 0), str(res))
        for stmt in ("insert into public.placements values (1,true)", "truncate public.cards_cache"):
            tr = c.transaction()
            await tr.start()
            try:
                await c.execute(f"SET LOCAL ROLE {role}")
                await c.execute(stmt)
                rep(f"{role} cannot write ({stmt[:24]})", False)
            except Exception as e:
                rep(f"{role} cannot write ({stmt[:24]})", type(e).__name__ == "InsufficientPrivilegeError", type(e).__name__)
            finally:
                await tr.rollback()
    after = await runtime_fingerprint(c)
    for role in ("lounge_api", "lounge_bot"):
        rep(f"{role} table/column/sequence privileges identical to before", after[role]["grants"] == BEFORE[role]["grants"], f"{len(after[role]['grants'])} grants")
        rep(f"{role} RLS policies identical to before", after[role]["policies"] == BEFORE[role]["policies"], f"{len(after[role]['policies'])} policies")
    return ok


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-railway", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback")
    a = ap.parse_args()
    if a.from_railway:
        from scripts.perf_baseline import load_from_railway
        load_from_railway()
    c = await connect()
    try:
        if a.rollback:
            stmts = rollback_sql(json.loads(Path(a.rollback).read_text()))
            await run_sql(c, stmts)
            print("rolled back", len(stmts), "statements")
            return
        snap = await snapshot(c)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bdir = ROOT / "backups"
        snapf = bdir / f"exposure-acl-snapshot-{stamp}.json"
        snapf.write_text(json.dumps(snap, indent=1))
        (bdir / f"exposure-rollback-{stamp}.sql").write_text(";\n".join(rollback_sql(snap)) + ";\n")
        print("snapshot:", snapf.name, "| rollback sql written | statements to apply:", len(APPLY))
        if not a.apply:
            print("dry-run: nothing changed")
            return
        u = urlsplit(os.environ["DATABASE_URL"])
        env = dict(os.environ, PGPASSWORD=unquote(u.password or ""), PGSSLMODE="verify-full", PGSSLROOTCERT=os.environ["DB_CA_FILE"])
        dump = bdir / f"exposure-tables-{stamp}.dump"
        cmd = [str(ROOT / ".local/postgres/bin-dist/bin/pg_dump.exe"), "-h", u.hostname, "-p", str(u.port or 5432), "-U", unquote(u.username),
               "-d", u.path.strip("/"), "-Fc", "--no-owner", "-f", str(dump)]
        for t in ALL_T:
            cmd += ["-t", f"public.{t}"]
        subprocess.run(cmd, env=env, check=True)
        print("data backup:", dump.name, dump.stat().st_size, "bytes")
        before = {t: await c.fetchval(f"select count(*) from public.{q(t)}") for t in ALL_T}
        BEFORE.update(await runtime_fingerprint(c))
        await run_sql(c, APPLY)
        print("applied", len(APPLY), "statements")
        if not await verify(c):
            print("VERIFICATION FAILED -> rolling back")
            await run_sql(c, rollback_sql(snap))
            raise SystemExit(2)
        after = {t: await c.fetchval(f"select count(*) from public.{q(t)}") for t in ALL_T}
        print("row counts unchanged:", before == after)
    finally:
        await c.close()


asyncio.run(main())
