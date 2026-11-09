"""Panel-friendly launcher:  python -u bootstrap.py <script.py> [args]

* Installs the pinned bot dependencies ONCE into ./.pydeps (binary wheels only, no compiling, hard timeout) and
  stamps the requirements hash, so later starts skip pip entirely.
* Puts ./.pydeps first on PYTHONPATH (children such as main.py inherit it), prints progress unbuffered, then
  replaces itself with the target script.
"""
import hashlib, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQ = HERE / "requirements-bot.txt"
DEPS = HERE / ".pydeps"
STAMP = DEPS / ".installed"
PIP_TIMEOUT = int(os.environ.get("BOOTSTRAP_PIP_TIMEOUT", "420"))


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


def log(m):
    print(f"[bootstrap {time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: bootstrap.py <script.py> [args]")
    want = hashlib.sha256(REQ.read_bytes() + sys.version.encode()).hexdigest()
    if STAMP.exists() and STAMP.read_text().strip() == want:
        log("dependencies already installed (stamp matches) -> skipping pip")
    else:
        log(f"installing dependencies into {DEPS.name} (python {sys.version.split()[0]}) ...")
        t = time.time()
        cmd = [sys.executable, "-m", "pip", "install", "--target", str(DEPS), "--only-binary=:all:", "--no-cache-dir",
               "--disable-pip-version-check", "--progress-bar", "off", "--upgrade", "-r", str(REQ)]
        try:
            r = subprocess.run(cmd, timeout=PIP_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SystemExit(f"pip exceeded {PIP_TIMEOUT}s (network/disk?). Re-run; completed packages are reused.")
        if r.returncode != 0:
            raise SystemExit(f"pip failed rc={r.returncode}")
        STAMP.write_text(want)
        log(f"installed in {time.time()-t:.0f}s")
    loaded = load_env_file(HERE / ".env")
    log(".env: " + ("not found next to bootstrap.py (variables must come from the panel environment)" if loaded is None else f"loaded {len(loaded)} variable(s): {', '.join(sorted(loaded)) or 'none new'}"))
    ca_candidate = HERE / "certs" / "supabase_ca_bundle.crt"
    if ca_candidate.exists() and not os.environ.get("DB_CA_FILE"):
        os.environ["DB_CA_FILE"] = str(ca_candidate.resolve())
        log("DB_CA_FILE resolved automatically to certs/supabase_ca_bundle.crt")
    if not os.environ.get("GUILD_ID"):
        os.environ["GUILD_ID"] = "1371306026917298267"
        log("GUILD_ID defaulted to production 1371306026917298267")
    if not os.environ.get("APPLICATION_ID"):
        os.environ["APPLICATION_ID"] = "1371546746248761455"
        log("APPLICATION_ID defaulted to production 1371546746248761455")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(DEPS), str(HERE), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    env["PYTHONUNBUFFERED"] = "1"
    target = [sys.executable, "-u", *sys.argv[1:]]
    log("starting: " + " ".join(sys.argv[1:]))
    os.chdir(HERE)
    if os.name == "nt":
        raise SystemExit(subprocess.call(target, env=env))
    os.execve(sys.executable, target, env)


main()
