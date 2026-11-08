from pathlib import Path
text = Path("scripts/live_pjsk_page.html").read_text(encoding="utf-8", errors="ignore")

idx = text.find("Season 4 scheduled end passed")
print(text[idx-500:idx+1500])

