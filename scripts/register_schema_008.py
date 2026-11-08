"""Register 008 in lounge_schema_migrations. ONLY after every 007-only runtime (Railway bot/API) is gone:
the old code requires max(version)=007 and would refuse to start afterwards."""
import argparse, asyncio, hashlib, os, ssl, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--from-railway", action="store_true")
    ap.add_argument("--confirm-no-007-runtime", action="store_true"); a = ap.parse_args()
    if a.from_railway:
        from scripts.perf_baseline import load_from_railway; load_from_railway()
    import asyncpg
    local = "127.0.0.1" in os.environ["DATABASE_URL"]
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=False if local else ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    assert await c.fetchval("SELECT to_regclass('public.lounge_room_reroll')"), "apply migration 008 first"
    if not a.confirm_no_007_runtime:
        print("dry-run: pass --confirm-no-007-runtime once Railway no longer runs 007-only code"); return
    sql = (ROOT / "migrations" / "008_room_reroll.sql").read_bytes()
    await c.execute("INSERT INTO lounge_schema_migrations(version,checksum) VALUES('008_room_reroll.sql',$1) ON CONFLICT DO NOTHING", hashlib.sha256(sql).hexdigest())
    print("registered; max version:", await c.fetchval("SELECT max(version) FROM lounge_schema_migrations"))
asyncio.run(main())
