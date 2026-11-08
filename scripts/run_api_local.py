"""Run the API bundle locally against Supabase via the TRANSACTION pooler (as Vercel will). Secrets only in memory."""
import os, secrets, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from scripts.perf_baseline import load_from_railway
load_from_railway()
bundle = Path(sys.argv[1]).resolve(); port = sys.argv[2] if len(sys.argv) > 2 else "8765"
u = urlsplit(os.environ["DATABASE_URL"])
os.environ["DATABASE_URL"] = urlunsplit(u._replace(netloc=u.netloc.rsplit(":", 1)[0] + ":6543"))
os.environ.update(DB_POOL_MODE="transaction", DB_POOL_MAX="1", DB_SSL="true", DB_CA_FILE=str(bundle / "certs" / "supabase_ca_bundle.crt"),
                  SESSION_SECRET=secrets.token_urlsafe(48), VERCEL="1", FRONT_URL=f"http://127.0.0.1:{port}", CORS_ORIGIN=f"http://127.0.0.1:{port}",
                  DISCORD_REDIRECT_URI=f"http://127.0.0.1:{port}/auth02/callback", DISCORD_CLIENT_ID="1371546746248761455")
os.chdir(bundle)
subprocess.run([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", port, "--log-level", "warning"])
