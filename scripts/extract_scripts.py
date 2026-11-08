from pathlib import Path
import re

text = Path(r"C:\Users\Usuario\pjsk-page\index.html").read_text(encoding="utf-8", errors="ignore")

# Extract the script block
scripts = re.findall(r"<script[\s\S]*?</script>", text)
print(f"Total script blocks: {len(scripts)}")
for idx, s in enumerate(scripts):
    print(f"--- Script {idx} (len {len(s)}) ---")
    if "Vue" in s or "displaySeason" in s or "seasonLabel" in s:
        # Save or inspect this script block
        Path(f"scripts/extracted_script_{idx}.js").write_text(s, encoding="utf-8")
        print(f"  Saved script {idx} to scripts/extracted_script_{idx}.js")

