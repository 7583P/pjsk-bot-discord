"""Lightweight source checks; only filenames are reported if a secret pattern matches."""

from pathlib import Path
import re
import subprocess


def check():
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], text=True
    ).splitlines()
    token = re.compile(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"
    )
    hits = []
    for name in paths:
        path = Path(name)
        if not path.is_file() or path.suffix in (".lnk", ".pyc"):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if token.search(content):
            hits.append(name)
    if hits:
        raise SystemExit("Posibles tokens: " + ", ".join(hits))
    if subprocess.check_output(
        ["git", "ls-files", ".env", "*.db", "*.dump"], text=True
    ).strip():
        raise SystemExit("Secretos o datos runtime incluidos en el índice Git")
    print(
        f"Source secret-pattern scan passed ({len(paths)} files); no runtime databases indexed."
    )


if __name__ == "__main__":
    check()
