"""Run ON the host: `python -u bootstrap.py probe.py`  (needs DATABASE_URL; never prints credentials).

Every step has its own timeout, output is line-buffered and a watchdog aborts with the exact stuck step,
so a hang can never hide the other results.  Read-only against the database.
"""
import asyncio, hashlib, json, os, platform, socket, ssl, statistics, sys, threading, time
from pathlib import Path
from urllib.parse import urlsplit

sys.stdout.reconfigure(line_buffering=True)
HERE = Path(__file__).resolve().parent
for extra in (HERE / ".pydeps",):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))
T0 = time.perf_counter()
STEP = {"name": "start"}
RESULTS: dict = {}


def load_env_file(path):
    """Minimal .env reader (KEY=VALUE, `export `, quotes, comments). Does not override real env vars; never prints values."""
    loaded = []
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


import re

_SECRETS = []  # exact secret strings to scrub from any output (DSN, password)


def redact(text):
    t = str(text)
    for sec in _SECRETS:
        if sec:
            t = t.replace(sec, "<redacted>")
    t = re.sub(r"//[^/\s@]*@", "//<redacted>@", t)       # any user:password@ userinfo
    t = re.sub(r"(?i)(password|passwd|pwd)=\S+", lambda m: m.group(1) + "=<redacted>", t)
    return t[:160]


def _safe_excepthook(etype, value, tb):
    print(f"FATAL {etype.__name__}: {redact(value)} (traceback suppressed to avoid leaking credentials)")
    sys.exit(1)


sys.excepthook = _safe_excepthook

PLACEHOLDERS = ("USUARIO", "SERVIDOR", "CONTRASE", "EJEMPLO", "YOUR_", "TU_", "HOSTNAME", "<", ">", "[", "]", "{", "}")


def check_dsn(url):
    """Syntax problems of DATABASE_URL, described WITHOUT echoing any part of the value."""
    problems = []
    if url != url.strip():
        problems.append("leading/trailing whitespace")
    if chr(10) in url or chr(13) in url or " " in url:
        problems.append("contains spaces or line breaks (several lines pasted into one value?)")
    if url.upper().lstrip().startswith("DATABASE_URL="):
        problems.append("the value itself starts with 'DATABASE_URL=' (key duplicated)")
    if url.count("://") != 1:
        problems.append(f"expected exactly one '://', found {url.count('://')}")
    found = [ph for ph in PLACEHOLDERS if ph.lower() in url.lower()]
    if found:
        problems.append(f"looks like an example string: contains placeholder marker(s) {found}")
    try:
        u = urlsplit(url.strip())
        if u.scheme not in ("postgres", "postgresql"):
            problems.append(f"scheme must be postgresql:// (got '{u.scheme}')")
        if not u.hostname:
            problems.append("no host")
        elif not u.hostname.endswith(".pooler.supabase.com"):
            problems.append(f"host '{u.hostname}' is not a Supabase pooler host")
        if not u.username or "." not in u.username:
            problems.append("user must look like <role>.<project-ref> for the Supabase pooler")
        if not u.password:
            problems.append("no password in the URL")
        if u.port not in (5432, 6543):
            problems.append(f"port must be 5432 (session) or 6543 (transaction), got {u.port}")
        if u.path.strip("/") != "postgres":
            problems.append("database name should be 'postgres'")
        if u.password:
            _SECRETS.append(u.password)
    except ValueError:
        problems.append("URL cannot be parsed (stray brackets/characters)")
    _SECRETS.append(url)
    return problems


def say(msg):
    print(f"[{time.perf_counter()-T0:6.1f}s] {msg}")


def step(name):
    STEP["name"] = name
    say(f"--- {name}")


def watchdog(limit=150):
    def run():
        time.sleep(limit)
        say(f"WATCHDOG: still running after {limit}s; stuck at step '{STEP['name']}'")
        os._exit(3)
    threading.Thread(target=run, daemon=True).start()


def pct(v, q):
    s = sorted(v)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def read(p):
    try:
        return Path(p).read_text().strip()
    except Exception:
        return None


def rss_mb():
    try:
        for l in open("/proc/self/status"):
            if l.startswith("VmRSS"):
                return int(l.split()[1]) / 1024
    except Exception:
        pass
    return None


def env_step():
    step("1. environment / limits")
    loaded = load_env_file(HERE / ".env")
    say(".env file: " + ("NOT FOUND in " + str(HERE) if loaded is None else f"found, loaded now: {sorted(loaded) or 'nothing new (already in process env)'}"))
    for k in ("DATABASE_URL", "DISCORD_TOKEN", "APPLICATION_ID", "GUILD_ID", "DB_SSL", "DB_POOL_MODE", "DB_POOL_MAX"):
        v = os.environ.get(k)
        say(f"env {k}: {'PRESENT' if v else 'ABSENT'}" + (f" (length {len(v)})" if v and k in ("DATABASE_URL", "DISCORD_TOKEN") else (f" = {v}" if v and k not in ('DATABASE_URL','DISCORD_TOKEN') else "")))
    say(f"python {platform.python_version()} | {platform.platform()} | machine={platform.machine()}")
    say(f"cpu_count={os.cpu_count()} loadavg={os.getloadavg() if hasattr(os, 'getloadavg') else 'n/a'}")
    mem = read("/sys/fs/cgroup/memory.max") or read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    cpu = read("/sys/fs/cgroup/cpu.max") or read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    say(f"cgroup memory limit={mem} cpu limit={cpu} | rss now={rss_mb() and round(rss_mb())}MB")
    if hasattr(os, "statvfs"):
        disk = os.statvfs(str(HERE))
        say(f"disk free={disk.f_bavail*disk.f_frsize/2**20:.0f}MB")


def package_step():
    step("2. package integrity / code version")
    mf = HERE / "MANIFEST.json"
    if mf.exists():
        want = {k.replace("\\", "/"): v for k, v in json.loads(mf.read_text()).items()}  # tolerate manifests written on Windows
        missing, differ = [], []
        for f, h in want.items():
            if f == "MANIFEST.json":
                continue
            fp = HERE.joinpath(*f.split("/"))
            if not fp.exists():
                missing.append(f)
            elif hashlib.sha256(fp.read_bytes()).hexdigest() != h:
                differ.append(f)
        RESULTS["manifest_ok"] = not missing and not differ
        say(f"MANIFEST {'PASS' if RESULTS['manifest_ok'] else 'FAIL'}: files={len(want)} missing={len(missing)} content-differs={len(differ)} {(missing + differ)[:6]}")
    else:
        say("MANIFEST.json missing (package not uploaded as built)")
    say(f"cogs/reroll.py present={ (HERE/'cogs'/'reroll.py').exists() } | lounge/perf.py present={ (HERE/'lounge'/'perf.py').exists() } | run_bot.py present={ (HERE/'run_bot.py').exists() }")
    for m in ("asyncpg", "discord", "aiohttp", "PIL", "fontTools", "dotenv", "pycountry"):
        try:
            mod = __import__(m)
            say(f"import {m} ok {getattr(mod, '__version__', '')}")
        except Exception as e:
            say(f"import {m} FAILED: {type(e).__name__}: {e}")


def dns_tls_step(host, port):
    step("3. DNS / TCP / TLS")
    for h in ("discord.com", "gateway.discord.gg", host):
        t = time.perf_counter()
        try:
            infos = socket.getaddrinfo(h, 443 if h != host else port, type=socket.SOCK_STREAM)
            fams = sorted({("IPv6" if i[0] == socket.AF_INET6 else "IPv4") for i in infos})
            say(f"DNS {h}: {len(infos)} addrs {fams} in {(time.perf_counter()-t)*1000:.0f} ms")
        except Exception as e:
            say(f"DNS {h} FAILED: {e}")
    cafile = HERE / "certs" / "supabase_ca_bundle.crt"
    for p in sorted({port, 5432, 6543}):
        t = time.perf_counter()
        try:
            s = socket.create_connection((host, p), timeout=10)
            say(f"TCP {host}:{p} connect {(time.perf_counter()-t)*1000:.0f} ms")
            s.close()
        except Exception as e:
            say(f"TCP {host}:{p} FAILED: {type(e).__name__}: {e}")
    say("(TLS to Postgres is negotiated by the driver in the next step, verified against certs/supabase_ca_bundle.crt)")
    return cafile


async def db_step(url, cafile):
    step("4. Supabase (PostgreSQL) latency")
    import asyncpg
    u = urlsplit(url)
    say(f"target host={u.hostname} port={u.port} db={u.path.strip('/')} user={(u.username or '').split('.')[0]} ref={'.'.join((u.username or '').split('.')[1:]) or '-'} (password hidden)")
    t = time.perf_counter()
    c = await asyncpg.connect(url, ssl=ssl.create_default_context(cafile=str(cafile)), statement_cache_size=0 if (u.port == 6543) else 100, timeout=20)
    say(f"connect (TLS verified + auth) {(time.perf_counter()-t)*1000:.0f} ms")
    await c.execute("SET default_transaction_read_only=on")
    say(f"current_user={await c.fetchval('select current_user')} superuser={await c.fetchval('select rolsuper from pg_roles where rolname=current_user')} server={await c.fetchval('show server_version')}")
    open_rooms = await c.fetchval("select count(*) from lounge_room_state where status in ('active','needs_attention')")
    say(f"season={await c.fetchval('select name from seasons where active order by startdate desc limit 1')} | open rooms={open_rooms}")
    xs = []
    for _ in range(40):
        t = time.perf_counter(); await c.fetchval("SELECT 1"); xs.append((time.perf_counter() - t) * 1000)
    say(f"SELECT 1 RTT p50={pct(xs,.5):.1f} p95={pct(xs,.95):.1f} p99={pct(xs,.99):.1f} ms (n=40)")
    RESULTS["rtt_p50"] = pct(xs, .5)
    ys = []
    for _ in range(10):
        t = time.perf_counter()
        await c.fetch("SELECT sp.user_id,sp.mmr FROM seasonplayers sp WHERE sp.season_id=(SELECT id FROM seasons WHERE active ORDER BY startdate DESC LIMIT 1) ORDER BY sp.mmr DESC LIMIT 100")
        ys.append((time.perf_counter() - t) * 1000)
    say(f"top100-like query p50={pct(ys,.5):.1f} p95={pct(ys,.95):.1f} ms (n=10)")
    await c.close()


def discord_step():
    step("5. Discord HTTP reachability")
    import urllib.request, urllib.error
    ts = []
    for i in range(5):
        t = time.perf_counter()
        try:
            req = urllib.request.Request("https://discord.com/api/v10/gateway", headers={"User-Agent": "DiscordBot (https://pjsk-page.vercel.app, 1.0) probe"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ts.append((time.perf_counter() - t) * 1000)
                if i == 0:
                    say(f"GET /gateway -> {r.status} ratelimit-remaining={r.headers.get('x-ratelimit-remaining')} server={r.headers.get('server')}")
        except urllib.error.HTTPError as e:
            say(f"GET /gateway HTTP {e.code} retry-after={e.headers.get('retry-after')} (429/1015 would mean the shared IP is limited)")
            return
        except Exception as e:
            say(f"GET /gateway FAILED: {type(e).__name__}: {e}")
            return
    say(f"Discord REST RTT p50={pct(ts,.5):.0f} max={max(ts):.0f} ms (n=5)")


def cpu_step():
    step("6. CPU (35% quota?) and image rendering")
    t = time.perf_counter(); sum(i * i for i in range(3_000_000)); loop = (time.perf_counter() - t) * 1000
    say(f"cpu loop 3M iterations: {loop:.0f} ms  [reference dev PC ~230 ms]")
    c0, w0 = time.process_time(), time.perf_counter()
    try:
        sys.path.insert(0, str(HERE))
        from helpers.leaderboard import generate_top100_image
        rows = [dict(user_id=i, member_name=f"Player {i}", player_name="x", role="Diamond", visible_mmr=9000 - i, title_label="Season 4 TOP 10", title_style="top-white") for i in range(100)]
        generate_top100_image(rows, "Season 5 (New Era)", 0)
        ts = []
        for pg in range(1, 4):
            t = time.perf_counter(); generate_top100_image(rows, "Season 5 (New Era)", pg); ts.append((time.perf_counter() - t) * 1000)
        say(f"top100 page render: {statistics.median(ts):.0f} ms median (n=3)  [dev PC ~160 ms]")
    except Exception as e:
        say(f"render FAILED: {type(e).__name__}: {e}")
    cpu_used, wall = time.process_time() - c0, time.perf_counter() - w0
    say(f"cpu seconds used / wall seconds = {cpu_used/wall:.2f} (about 0.35 means the quota is throttling)")
    try:
        import resource
        say(f"peak RSS={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB (limit 512)")
    except ImportError:
        say(f"RSS={rss_mb()}")


def main():
    watchdog()
    for fn in (env_step, package_step):
        try:
            fn()
        except Exception as e:
            say(f"{fn.__name__} failed: {type(e).__name__}: {redact(e)}")
    url = os.environ.get("DATABASE_URL")
    if not url:
        say("FAIL: DATABASE_URL is not set (checked process env and .env) -> database checks NOT performed")
        RESULTS["db_ok"] = False
    else:
        problems = check_dsn(url)
        step("DATABASE_URL syntax")
        if problems:
            for pr in problems:
                say(f"FAIL DATABASE_URL: {pr}")
            RESULTS["db_ok"] = False
            url = None
        else:
            say("DATABASE_URL syntax OK (scheme, Supabase pooler host, <role>.<ref> user, password present, port, db name)")
    if url:
        u = urlsplit(url)
        cafile = dns_tls_step(u.hostname, u.port or 5432)
        try:
            asyncio.run(asyncio.wait_for(db_step(url, cafile), timeout=90))
            RESULTS["db_ok"] = True
        except Exception as e:
            RESULTS["db_ok"] = False
            say(f"DB step FAILED: {type(e).__name__}: {redact(e)}")
    try:
        discord_step()
    except Exception as e:
        say(f"discord step failed: {e}")
    try:
        cpu_step()
    except Exception as e:
        say(f"cpu step failed: {type(e).__name__}: {e}")
    ok = RESULTS.get("db_ok") and RESULTS.get("manifest_ok", False)
    say(f"SUMMARY: manifest={'PASS' if RESULTS.get('manifest_ok') else 'FAIL'} database={'PASS' if RESULTS.get('db_ok') else 'FAIL'}")
    say("PROBE COMPLETE" if ok else "PROBE INCOMPLETE (see FAIL lines above)")
    sys.exit(0 if ok else 1)


main()
