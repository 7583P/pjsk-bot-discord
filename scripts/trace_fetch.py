from pathlib import Path
text = Path("web/index.html").read_text(encoding="utf-8")
idx1 = text.find("async fetchSeasons()")
print("--- fetchSeasons ---")
print(text[idx1:idx1+800].encode('ascii', 'ignore').decode('ascii'))
idx2 = text.find("async fetchPlayers()")
print("\n--- fetchPlayers ---")
print(text[idx2:idx2+1200].encode('ascii', 'ignore').decode('ascii'))

