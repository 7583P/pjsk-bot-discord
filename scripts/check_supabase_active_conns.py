import asyncio
import ssl
from pathlib import Path
import asyncpg

cred_path = Path(r"C:\Users\Usuario\.pjsk_secure_backups\lounge_credentials.env")
creds = {}
with open(cred_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")

db_url = creds.get("SUPABASE_POOLER_URL") or creds.get("SUPABASE_DATABASE_URL")
ca_path = Path(r"d:\Projects\PJSK Bot\certs\supabase_ca_bundle.crt")

ssl_ctx = ssl.create_default_context(cafile=str(ca_path))
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_REQUIRED

async def check():
    conn = await asyncpg.connect(db_url, ssl=ssl_ctx, statement_cache_size=0)
    try:
        rows = await conn.fetch("""
            SELECT client_addr, application_name, state, count(*) as count
            FROM pg_stat_activity
            GROUP BY client_addr, application_name, state;
        """)
        print("=== CURRENT ACTIVE PG CONNECTIONS IN SUPABASE ===")
        total = 0
        for r in rows:
            print(f"  App: {r['application_name']}, State: {r['state']}, Addr: {r['client_addr']} -> Count: {r['count']}")
            total += r['count']
        print(f"Total connections: {total} / 15 pool_size limit")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(check())

