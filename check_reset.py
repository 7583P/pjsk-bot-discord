import sqlite3

DB_PATH = 'matchmaking.db'
conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

cur.execute("SELECT COUNT(*) FROM players WHERE mmr = 0 AND role = 'Placement';")
placement_zero = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM players;")
total = cur.fetchone()[0]

conn.close()

print(f"Players con mmr=0 y role='Placement': {placement_zero}")
print(f"Total de players en la tabla:           {total}")
