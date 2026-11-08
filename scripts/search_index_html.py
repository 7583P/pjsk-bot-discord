from pathlib import Path
import re

p = Path(r"C:\Users\Usuario\pjsk-page\index.html")
text = p.read_text(encoding="utf-8", errors="ignore")

# Find select elements or season rendering
for match in re.finditer(r"(season|select|option|aviso|confirm)", text, re.IGNORECASE):
    snippet = text[max(0, match.start() - 30):min(len(text), match.end() + 50)]
    # print sample snippets
    if any(k in snippet.lower() for k in ["season", "option", "select", "aviso", "confirm"]):
        print(repr(snippet.strip()))

