from pathlib import Path
import re
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("data(){")
print(text[idx:idx+800].encode('ascii', 'ignore').decode('ascii'))

