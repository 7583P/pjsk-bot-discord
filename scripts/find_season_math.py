from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")

# Find occurrences where season numbers are manipulated
matches = re.findall(r"[^\n]+(?:\+ 1|\+1|seasonNumber|season_number|seasonIndex|\.length)[^\n]+", text, re.IGNORECASE)
for m in matches[:25]:
    if any(k in m.lower() for k in ["season", "era"]):
        print(m.strip().encode('ascii', 'ignore').decode('ascii'))

