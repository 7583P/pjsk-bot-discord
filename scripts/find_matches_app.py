from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("MATCHES GLOBAL APP")
print(text[idx+100:idx+2500].encode('ascii', 'ignore').decode('ascii'))

