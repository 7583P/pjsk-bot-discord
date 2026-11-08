from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("populateSeasonTitleSelector")
if idx >= 0:
    print(text[idx-50:idx+900].encode('ascii', 'ignore').decode('ascii'))
else:
    for m in re.finditer(r"seasonTitleSelect", text):
        pos = m.start()
        print(text[pos-30:pos+300].encode('ascii', 'ignore').decode('ascii'))

