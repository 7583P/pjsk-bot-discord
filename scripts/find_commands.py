with open('cogs/matchmaking.py', encoding='utf-8') as f:
    for i, line in enumerate(f):
        if 'name="cutoffs"' in line or 'name="mmr"' in line or 'name="c"' in line or 'name="d"' in line:
            print(f"{i+1}: {line.strip()}")

