"""Server-side checks after the human OAuth login (read-only): lounge_api connections, OAuth state hygiene, runtime errors."""
import asyncio, os, ssl, subprocess, sys, re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from scripts.perf_baseline import load_from_railway
load_from_railway()
import asyncpg
async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=os.environ["DB_CA_FILE"]), statement_cache_size=0)
    await c.execute("SET default_transaction_read_only=on")
    print("connections by role:", {r["usename"]: r["n"] for r in await c.fetch("select usename, count(*) n from pg_stat_activity where datname=current_database() group by 1")})
    print("lounge_api connection limit:", await c.fetchval("select rolconnlimit from pg_roles where rolname='lounge_api'"), "| max_connections:", await c.fetchval("show max_connections"))
    print("pending oauth states (unconsumed, unexpired):", await c.fetchval("select count(*) from lounge_oauth_states where expires_at>now()"))
    print("revoked tokens (logout/rotation):", await c.fetchval("select count(*) from lounge_revoked_tokens"))
    await c.close()
    bundle = sorted((ROOT / "artifacts").glob("vercel-preview-*"))[-1]
    r = subprocess.run(["npx.cmd", "--yes", "vercel@latest", "logs", "--environment", "preview", "--since", "2h", "--no-follow", "--json"], capture_output=True, text=True, cwd=bundle)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    codes = {}
    for l in lines:
        m = re.search(r'"statusCode":\s*(\d+)', l)
        if m: codes[m.group(1)] = codes.get(m.group(1), 0) + 1
    errs = [l for l in lines if re.search(r'(?i)"level":\s*"error"|Traceback|permission denied|EMAXCONN|too many', l)]
    print("runtime log lines:", len(lines), "| status codes:", codes, "| error-like lines:", len(errs))
    for l in errs[:5]: print("  ", re.sub(r"(postgres(ql)?://)[^@\s]+@", r"\1<redacted>@", l)[:240])
asyncio.run(main())
