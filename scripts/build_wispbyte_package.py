"""Build a clean, secret-free deploy folder + zip for Wispbyte (bot only)."""
import datetime, hashlib, json, re, shutil, sys, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
dest = ROOT / "artifacts" / f"wispbyte-package-{stamp}"
assert not dest.exists()
INCLUDE_FILES = ["main.py", "run_bot.py", "probe.py", "bootstrap.py", "requirements-bot.txt", ".env.example"]
EXTRA_COPIES = {"requirements-bot.txt": "requirements.txt"}  # default panel installers look for requirements.txt
INCLUDE_DIRS = ["cogs", "lounge", "helpers", "assets", "certs", "migrations"]
SKIP = {"__pycache__"}
SKIP_FILES = {"remove_user.py", "ping_test.py", "mmr.py", "placement.py", "seasons.py", "autoroles.py_"}  # not loaded by main.py cogs list
dest.mkdir(parents=True)
for f in INCLUDE_FILES:
    shutil.copy2(ROOT / f, dest / f)
for src, dst in EXTRA_COPIES.items():
    shutil.copy2(ROOT / src, dest / dst)
for d in INCLUDE_DIRS:
    for p in (ROOT / d).rglob("*"):
        if p.is_file() and not (set(p.parts) & SKIP) and p.suffix not in {".pyc", ".lnk"}:
            if d == "cogs" and p.name in SKIP_FILES:
                continue
            t = dest / p.relative_to(ROOT); t.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, t)
# secret scan
pat = re.compile(r"[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}|postgres(ql)?://[^:@/\s]+:[^@\s]{6,}@|sb_secret_|eyJ[A-Za-z0-9_-]{20,}\.eyJ")
bad = [str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() and p.suffix in {".py", ".txt", ".md", ".json", ".sql", ".example", ""} and pat.search(p.read_text(errors="ignore"))]
assert not bad, f"secret-like content in {bad}"
assert not list(dest.rglob(".env")) and not list(dest.rglob("*.db")) and not list(dest.rglob("*.dump"))
manifest = {p.relative_to(dest).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dest.rglob("*")) if p.is_file()}
(dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
z = dest.with_suffix(".zip")
with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(dest.rglob("*")):
        if p.is_file(): zf.write(p, p.relative_to(dest))
print(dest, len(manifest), "files", round(z.stat().st_size / 1e6, 2), "MB")
