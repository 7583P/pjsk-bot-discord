from pathlib import Path
import re

for s_name in ["scripts/extracted_script_2.js", "scripts/extracted_script_3.js"]:
    p = Path(s_name)
    if not p.exists(): continue
    text = p.read_text(encoding="utf-8", errors="ignore")
    print(f"\n=================== {s_name} ===================")
    for kw in ["displaySeason", "seasonLabel", "seasons", "Season 4", "Season 5", "Season 6", "confirm", "placement", "filter"]:
        matches = [m.start() for m in re.finditer(re.escape(kw), text, re.IGNORECASE)]
        print(f"Keyword '{kw}': {len(matches)} matches")
        for pos in matches[:3]:
            print(f"   [{kw}] ...{text[max(0, pos-40):min(len(text), pos+60)]!r}...")

