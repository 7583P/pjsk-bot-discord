from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")

for m in re.finditer(r"<select[\s\S]*?</select>", text):
    print("Found select element:", m.group(0)[:300].encode('ascii', 'ignore').decode('ascii'))

