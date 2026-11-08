from pathlib import Path
import re

for fname in ["scripts/extracted_script_2.js", "scripts/extracted_script_3.js"]:
    text = Path(fname).read_text(encoding="utf-8")
    print(f"=== {fname} ===")
    for m in re.finditer(r"(\.seasons[\w\.\(\)\[\]]*|seasons\s*=\s*[^;\n]+|seasons\.map[^;\n]+)", text):
        print("  ", m.group(0))

