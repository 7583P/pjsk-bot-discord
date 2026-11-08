from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("isZeroMmrUnplayedNonStanding(p){")
print(text[idx:idx+900])

