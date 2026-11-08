from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")
for m in re.finditer(r"displaySeasonMenuName\s*\(", text):
    pos = m.start()
    print("Definition:", text[pos-30:pos+400].encode('ascii', 'ignore').decode('ascii'))

