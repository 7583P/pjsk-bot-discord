from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("const cleaned = (resp.data || []).map")
print(text[idx:idx+1500].encode('ascii', 'ignore').decode('ascii'))

