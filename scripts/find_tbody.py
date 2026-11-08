from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("<tbody")
print(text[idx-50:idx+800].encode('ascii', 'ignore').decode('ascii'))

