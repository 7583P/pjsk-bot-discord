"""Run ON the Wispbyte server: `python probe.py` (needs DATABASE_URL; uses certs/supabase_ca_bundle.crt).
Prints latency/capacity facts only; never prints credentials. Read-only against the database."""
import asyncio, os, platform, ssl, statistics, sys, time, urllib.request

def pct(v, q):
    s = sorted(v); return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]

async def main():
    print("python", platform.python_version(), platform.platform())
    try:
        print("cpu_count", os.cpu_count(), "| mem_total_kb", next(l.split()[1] for l in open("/proc/meminfo") if l.startswith("MemTotal")))
    except Exception:
        pass
    # Discord REST reachability / rate-limit headers (no auth)
    t = time.perf_counter()
    try:
        with urllib.request.urlopen("https://discord.com/api/v10/gateway", timeout=15) as r:
            print(f"discord /gateway {r.status} in {(time.perf_counter()-t)*1000:.0f} ms; ratelimit-remaining={r.headers.get('x-ratelimit-remaining')}")
    except Exception as e:
        print("discord /gateway FAILED:", type(e).__name__, getattr(e, "code", ""), getattr(getattr(e, "headers", None), "get", lambda *_: "")("retry-after"))
    import asyncpg
    cafile = os.path.join(os.path.dirname(__file__), "certs", "supabase_ca_bundle.crt")
    t = time.perf_counter()
    c = await asyncpg.connect(os.environ["DATABASE_URL"], ssl=ssl.create_default_context(cafile=cafile), statement_cache_size=0, timeout=20)
    print(f"tls+auth connect {(time.perf_counter()-t)*1000:.0f} ms")
    await c.execute("SET default_transaction_read_only=on")
    xs = []
    for _ in range(30):
        t = time.perf_counter(); await c.fetchval("SELECT 1"); xs.append((time.perf_counter() - t) * 1000)
    print(f"SELECT 1 RTT  p50={pct(xs,.5):.1f} p95={pct(xs,.95):.1f} p99={pct(xs,.99):.1f} ms (n=30)")
    ys = []
    for _ in range(10):
        t = time.perf_counter()
        await c.fetch("SELECT sp.user_id,sp.mmr FROM seasonplayers sp WHERE sp.season_id=(SELECT id FROM seasons WHERE active ORDER BY startdate DESC LIMIT 1) ORDER BY sp.mmr DESC LIMIT 100")
        ys.append((time.perf_counter() - t) * 1000)
    print(f"top100-like query p50={pct(ys,.5):.1f} p95={pct(ys,.95):.1f} ms (n=10)")
    await c.close()
    t = time.process_time(); s = time.perf_counter(); sum(i * i for i in range(2_000_000))
    print(f"cpu benchmark (2M loop) {(time.perf_counter()-s)*1000:.0f} ms  [dev PC ~150 ms]")

asyncio.run(main())
