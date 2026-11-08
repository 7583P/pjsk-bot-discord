from pathlib import Path
import re
text = Path("scripts/live_pjsk_page.html").read_text(encoding="utf-8", errors="ignore")

for m in re.finditer(r"<script[^>]*src=[\"']([^\"']+)[\"']", text):
    print("Script src:", m.group(1))

