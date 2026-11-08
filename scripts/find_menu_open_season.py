from pathlib import Path
text = Path(r"C:\Users\Usuario\pjsk-page\index.html").read_text(encoding="utf-8", errors="ignore")
idx = text.find("menuOpenSeason")
snippet = text[idx-200:idx+1500]
print(snippet.encode('ascii', 'ignore').decode('ascii'))
