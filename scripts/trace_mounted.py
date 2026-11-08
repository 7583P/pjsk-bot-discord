from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("async mounted()")
print(text[idx:idx+1200].encode('ascii', 'ignore').decode('ascii'))

