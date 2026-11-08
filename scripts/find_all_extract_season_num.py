from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")
for m in re.finditer(r"extractSeasonNumber\s*\(", text):
    pos = m.start()
    print("Usage:", text[pos-40:pos+250].encode('ascii', 'ignore').decode('ascii'))

