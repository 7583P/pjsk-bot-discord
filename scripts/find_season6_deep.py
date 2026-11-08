import os
import re

for root, dirs, files in os.walk('.'):
    if any(k in root for k in ['.git', 'node_modules', '.venv', '__pycache__', '.local']):
        continue
    for f in files:
        if f.endswith(('.html', '.js', '.ts', '.py', '.json', '.sql', '.md')):
            p = os.path.join(root, f)
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as fp:
                    content = fp.read()
                    if re.search(r'Season\s*6', content, re.I):
                        print(f"Match 'Season 6' in {p}")
                    if re.search(r'6\s*\(New Era\)', content, re.I):
                        print(f"Match '6 (New Era)' in {p}")
            except Exception:
                pass

