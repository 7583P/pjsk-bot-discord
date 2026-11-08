from pathlib import Path
import re
text = Path("scripts/live_pjsk_page.html").read_text(encoding="utf-8", errors="ignore")

for m in re.finditer(r"explicitSeasonState\s*[:=\(]", text):
    pos = m.start()
    print("Definition:", text[pos-30:pos+300])

