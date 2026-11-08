from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx = text.find("axios.create")
print(text[idx-50:idx+400].encode('ascii', 'ignore').decode('ascii'))

