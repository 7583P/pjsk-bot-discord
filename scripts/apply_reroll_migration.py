"""Apply migration 008 to the database in DATABASE_URL (or --from-railway). Idempotent.

Pre-checks schema version/tables, uses a short lock_timeout so active rooms are never
blocked, records the run in lounge_operations (NOT lounge_schema_migrations: the
previously deployed runtime requires max(version)=007, so bumping it would break
Railway restarts/rollback).
"""
import argparse, asyncio, json, os, ssl, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--from-railway", action="store_true"); ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.from_railway:
        from scripts.perf_baseline import load_from_railway; load_from_railway()
    import asyncpg
    local = "127.0.0.1" in os.environ["DATABASE_URL"]
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=False if local else ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    try:
        ver = await c.fetchval("SELECT max(version) FROM lounge_schema_migrations")
        assert ver == "007_publication_state.sql", f"unexpected schema version {ver}"
        for t in ("lounge_room_state", "lounge_submissions", "seasons"):
            assert await c.fetchval("SELECT to_regclass($1)", "public." + t), t
        exists = await c.fetchval("SELECT to_regclass('public.lounge_room_reroll')")
        print("version", ver, "| table exists:", bool(exists))
        if not a.apply:
            print("dry-run: nothing changed"); return
        sql = (ROOT / "migrations" / "008_room_reroll.sql").read_text(encoding="utf-8")
        for attempt in range(5):
            try:
                async with c.transaction():
                    await c.execute("SET LOCAL lock_timeout='3s'")
                    await c.execute(sql)
                    await c.execute("INSERT INTO lounge_operations(id,kind,payload) VALUES('migration:008_room_reroll','migration',$1::jsonb) ON CONFLICT DO NOTHING",
                                    json.dumps({"file": "008_room_reroll.sql", "additive": True}))
                break
            except asyncpg.exceptions.LockNotAvailableError:
                print("lock busy, retry", attempt + 1); await asyncio.sleep(2)
        else:
            raise SystemExit("could not obtain lock; nothing applied")
        rls = await c.fetchval("SELECT relrowsecurity FROM pg_class WHERE relname='lounge_room_reroll'")
        pol = [r[0] for r in await c.fetch("SELECT policyname FROM pg_policies WHERE tablename='lounge_room_reroll'")]
        print("applied; RLS:", rls, "policies:", pol, "| max version still:", await c.fetchval("SELECT max(version) FROM lounge_schema_migrations"))
    finally:
        await c.close()
asyncio.run(main())
