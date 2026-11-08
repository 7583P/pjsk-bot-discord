from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("isZeroMmrUnplayedNonStanding")
print(text[idx-50:idx+600])

