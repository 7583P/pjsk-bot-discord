"""Read-only pg_dump of the production DB (URL from Railway CLI, never printed)."""
import hashlib, os, subprocess, sys, datetime
from pathlib import Path
from urllib.parse import urlsplit, unquote
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.perf_baseline import load_from_railway
load_from_railway()
u = urlsplit(os.environ["DATABASE_URL"])
env = dict(os.environ, PGPASSWORD=unquote(u.password or ""), PGSSLMODE="verify-full", PGSSLROOTCERT=str(ROOT / "certs" / "supabase_ca_bundle.crt"))
out = ROOT / "backups" / f"supabase-prod-{datetime.datetime.utcnow():%Y%m%dT%H%M%SZ}.dump"
cmd = [str(ROOT / ".local/postgres/bin-dist/bin/pg_dump.exe"), "-h", u.hostname, "-p", str(u.port or 5432), "-U", unquote(u.username), "-d", u.path.strip("/"),
       "-Fc", "--no-owner", "--no-privileges", "-n", "public", "-f", str(out)]
subprocess.run(cmd, env=env, check=True)
h = hashlib.sha256(out.read_bytes()).hexdigest()
Path(str(out) + ".sha256").write_text(h)
print(out.name, out.stat().st_size, h)
