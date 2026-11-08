from pathlib import Path
import re

text = Path("web/index.html").read_text(encoding="utf-8")

# Find all occurrences of {{ ...season... }} or {{ ...Season... }}
templates = re.findall(r"\{\{[^}]*(?:season|Season)[^}]*\}\}", text)
for t in set(templates):
    print("Template:", t.encode('ascii', 'ignore').decode('ascii'))

