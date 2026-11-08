from pathlib import Path
import re

text = Path("scripts/live_pjsk_page.html").read_text(encoding="utf-8", errors="ignore")
print("Length of live HTML:", len(text))

keywords = ["Season 6", "Season 5", "Season 4", "New Era", "espera", "confirm", "aviso", "leaderboard"]
for kw in keywords:
    matches = list(re.finditer(re.escape(kw), text, re.IGNORECASE))
    print(f"\nKeyword '{kw}': {len(matches)} matches")
    for m in matches[:5]:
        pos = m.start()
        print("  -->", repr(text[max(0, pos-50):min(len(text), pos+70)]))

