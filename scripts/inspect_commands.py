with open("cogs/matchmaking.py", "r", encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        if any(p in line for p in ['name="c"', "name='c'", 'name="d"', "name='d'", 'name="mmr"']):
            print(f"{i}: {line.strip()}")

