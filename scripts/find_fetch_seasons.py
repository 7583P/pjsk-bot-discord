from pathlib import Path
text = Path("scripts/extracted_script_2.js").read_text(encoding="utf-8")
idx = text.find("fetchSeasons")
print(text[idx-50:idx+800])

