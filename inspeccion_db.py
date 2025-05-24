import sqlite3, os

BASE    = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE, "matchmaking.db")

conn = sqlite3.connect(db_path)
cur  = conn.cursor()

# Tablas
cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
print("Tablas en la base:", cur.fetchall())

# players
cur.execute("PRAGMA table_info(players);")
print("\nEsquema de 'players':")
for col in cur.fetchall():
    print(col)

# placements
cur.execute("PRAGMA table_info(placements);")
print("\nEsquema de 'placements':")
for col in cur.fetchall():
    print(col)

conn.close()
