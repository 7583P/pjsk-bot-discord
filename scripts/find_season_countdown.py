from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("seasonCountdownText")
print(text[idx-200:idx+800])

