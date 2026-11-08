from pathlib import Path
import re

text = Path("scripts/extracted_script_3.js").read_text(encoding="utf-8")
print("Length of script 3:", len(text))
for m in re.finditer(r"(Season\s*\d|seasonId|isCurrentSeasonSelected|season)", text, re.IGNORECASE):
    pos = m.start()
    print("Match:", m.group(0), "-->", repr(text[max(0, pos-40):min(len(text), pos+60)]))

