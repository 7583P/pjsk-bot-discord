from pathlib import Path
import re
text = Path("scripts/live_pjsk_page.html").read_text(encoding="utf-8", errors="ignore")

for m in re.finditer(r"(PJSKClient\s*=|class PJSKClient|var PJSKClient|const PJSKClient|let PJSKClient|window\.PJSKClient)", text):
    pos = m.start()
    print("Found PJSKClient definition:", text[pos:pos+400])

