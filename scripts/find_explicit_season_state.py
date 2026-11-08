from pathlib import Path
text = Path("scripts/live_pjsk_page.html").read_text(encoding="utf-8", errors="ignore")

idx = text.find("explicitSeasonState")
print(text[idx-50:idx+600])

