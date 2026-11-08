import asyncio
import asyncpg
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

dump_path = Path(r"D:\Projects\PJSK Bot\backups\supabase_pre_s5.dump")
manifest_path = Path(r"D:\Projects\PJSK Bot\backups\supabase_pre_s5.manifest.json")

async def fingerprint(conn):
    tables = await conn.fetch("""
        SELECT tablename FROM pg_tables 
        WHERE schemaname='public' 
        AND tablename NOT IN ('lounge_runtime_lease', 'lounge_oauth_states', 'lounge_revoked_tokens', 'lounge_role_jobs')
        ORDER BY tablename
    """)
    state = {}
    for table in tables:
        name = table['tablename'].replace('"', '""')
        rows = await conn.fetch(f'SELECT to_jsonb(t)::text AS value FROM "{name}" t ORDER BY to_jsonb(t)::text')
        state[table['tablename']] = dict(
            count=len(rows),
            sha256=hashlib.sha256("\n".join(r['value'] for r in rows).encode()).hexdigest(),
        )
    return state

async def main():
    conn = await asyncpg.connect("postgresql://postgres:postgres@127.0.0.1:55432/pjsk_test_pre_s5_verify")
    state = await fingerprint(conn)
    await conn.close()
    
    data = {
        "format": "postgres-custom-v1",
        "restore_verified": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file": dump_path.name,
        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
        "state": state
    }
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Manifest created successfully! Tables: {len(state)}")

if __name__ == "__main__":
    asyncio.run(main())

