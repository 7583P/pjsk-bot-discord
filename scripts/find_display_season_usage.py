from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")
for m in re.finditer(r"displaySeason\s*\(", text):
    pos = m.start()
    print("Match:", text[pos-30:pos+300].encode('ascii', 'ignore').decode('ascii'))

