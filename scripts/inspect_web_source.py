from pathlib import Path
import re

p = Path(r"C:\Users\Usuario\pjsk-page\index.html")
if p.exists():
    text = p.read_text(encoding="utf-8", errors="ignore")
    print(f"Found {p}, length: {len(text)}")
    for m in re.finditer(r"(Season\s*\d|confirmaci|New Era)", text, re.IGNORECASE):
        start = max(0, m.start() - 40)
        end = min(len(text), m.end() + 60)
        print(f"[{m.group(0)}] Context: {text[start:end]!r}")
else:
    print(f"{p} does not exist")

