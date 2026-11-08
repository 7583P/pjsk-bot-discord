from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("extractSeasonNumber(value)")
print(text[idx-50:idx+900].encode('ascii', 'ignore').decode('ascii'))

