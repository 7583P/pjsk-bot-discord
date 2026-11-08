"""Set PREVIEW-scope env vars on the Vercel project from the Railway CLI session. Values go via stdin, never printed."""
import os, secrets, subprocess, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
ROOT = Path(__file__).resolve().parent.parent
bundle = Path(sys.argv[1]).resolve(); alias = sys.argv[2]
target = sys.argv[3] if len(sys.argv) > 3 else "preview"
if target == "production" and "--execute" not in sys.argv:
    raise SystemExit("production env changes require --execute (and an approved cutover window)")
out = subprocess.run("railway variables --kv", shell=True, capture_output=True, text=True, check=True).stdout
rv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
u = urlsplit(rv["DATABASE_URL"])
vals = {
    "DATABASE_URL": urlunsplit(u._replace(netloc=u.netloc.rsplit(":", 1)[0] + ":6543")),
    "DB_POOL_MODE": "transaction", "DB_POOL_MAX": "1", "DB_SSL": "true", "DB_CA_FILE": "certs/supabase_ca_bundle.crt",
    "SESSION_SECRET": secrets.token_urlsafe(48),           # preview-only, independent of production
    "DISCORD_CLIENT_ID": rv["DISCORD_CLIENT_ID"], "DISCORD_CLIENT_SECRET": rv["DISCORD_CLIENT_SECRET"],
    "COOKIE_NAME": rv.get("COOKIE_NAME", "pjsk_session"),
    "FRONT_URL": f"https://{alias}", "CORS_ORIGIN": f"https://{alias}", "APP_ORIGIN": f"https://{alias}",
    "DISCORD_REDIRECT_URI": f"https://{alias}/auth02/callback", "PJSK_BUILD_ID": "preview-" + bundle.name[-16:],
}
if target == "production":
    vals.pop("DATABASE_URL")  # set by scripts.enable_lounge_api_login --targets preview,production (rotates the password everywhere)
for k, v in vals.items():
    r = subprocess.run(["npx.cmd", "--yes", "vercel@latest", "env", "add", k, target, "--force", "--yes"] + (["--sensitive"] if k in ("DATABASE_URL", "SESSION_SECRET", "DISCORD_CLIENT_SECRET") else []),
                       input=v, text=True, capture_output=True, cwd=bundle)
    last = [l for l in (r.stdout + r.stderr).splitlines() if l.strip() and "notice" not in l]
    print(k, "rc", r.returncode, (last[-1][:110] if last else ""))
