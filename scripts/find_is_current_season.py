from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("isCurrentSeasonSelected()")
print(text[idx-50:idx+600].encode('ascii', 'ignore').decode('ascii'))

