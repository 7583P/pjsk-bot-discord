from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")
for m in re.finditer(r'<div class="dropdown"[^>]*>', text):
    pos = m.start()
    print("Dropdown:", text[pos:pos+400].encode('ascii', 'ignore').decode('ascii'))

