"""Build a Vercel (Python/FastAPI) preview bundle for project pjsk-page: API + static web. No secrets."""
import datetime, hashlib, json, re, shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
dest = ROOT / "artifacts" / f"vercel-preview-{stamp}"
dest.mkdir(parents=True)
shutil.copy2(ROOT / "app.py", dest / "app.py")
(dest / "lounge").mkdir()
for m in ("__init__", "config", "connections", "credentials", "diagnostics", "json_ids", "logging", "oauth_state", "perf", "schema", "web_security", "ttlcache"):
    shutil.copy2(ROOT / "lounge" / f"{m}.py", dest / "lounge" / f"{m}.py")
shutil.copy2(ROOT / "lounge" / "exposed_token_hashes.json", dest / "lounge")
(dest / "certs").mkdir(); shutil.copy2(ROOT / "certs" / "supabase_ca_bundle.crt", dest / "certs")
pub = dest / "public"; pub.mkdir()
web = ROOT / "web"
for f in ("index.html", "lounge-client.js"):
    shutil.copy2(web / f, pub / f)
shutil.copytree(web / "assets", pub / "assets")
(dest / "requirements.txt").write_text("fastapi==0.162.0\nasyncpg==0.32.0\nhttpx==0.28.1\npython-dotenv==1.2.4\npydantic==2.14.2\n")
(dest / "pyproject.toml").write_text('[project]\nname = "pjsk-lounge-api"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n')
(dest / "vercel.json").write_text(json.dumps({
    "framework": "fastapi",
    "functions": {"app.py": {"maxDuration": 30}},
    "headers": [{"source": "/", "headers": [{"key": "Cache-Control", "value": "no-store, must-revalidate"}]},
                {"source": "/index.html", "headers": [{"key": "Cache-Control", "value": "no-store, must-revalidate"}]}],
}, indent=2))
(dest / ".vercelignore").write_text("__pycache__\n*.pyc\n.env*\n")
pat = re.compile(r"[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}|postgres(ql)?://[^:@/\s]+:[^@\s]{6,}@|eyJ[A-Za-z0-9_-]{20,}\.eyJ")
bad = [str(p) for p in dest.rglob("*") if p.is_file() and p.suffix in {".py", ".js", ".html", ".json", ".txt", ".toml"} and pat.search(p.read_text(errors="ignore"))]
assert not bad, bad
(dest / "MANIFEST.json").write_text(json.dumps({str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dest.rglob("*")) if p.is_file()}, indent=1))
print(dest)
