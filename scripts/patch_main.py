with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

out = []
for line in lines:
    out.append(line)
    if 'logger.info(' in line and 'Conectado como' in lines[lines.index(line) + 1]:
        pass
    if 'self.user.id if self.user else "?",' in line:
        out.append("        )\n")
        out.append("        if GUILD_ID:\n")
        out.append("            try:\n")
        out.append("                guild = discord.Object(id=int(GUILD_ID))\n")
        out.append("                self.tree.copy_global_to(guild=guild)\n")
        out.append("                synced = await self.tree.sync(guild=guild)\n")
        out.append('                logger.info("Slash commands sincronizados en guild %s (%d): %s", GUILD_ID, len(synced), [c.name for c in synced])\n')
        out.append("            except Exception as e:\n")
        out.append('                logger.warning("Aviso sincronizando slash commands: %s", e)\n')
        out.append("        return\n")

# Wait, let's do exact target replace
with open("main.py", "r", encoding="utf-8") as f:
    text = f.read()

idx = text.find('logger.info(\n            "🤖 Conectado como')
if idx != -1:
    end_idx = text.find('bot = MyBot()', idx)
    part1 = text[:idx]
    part2 = text[end_idx:]
    new_method = '''logger.info(
            "🤖 Conectado como %s (ID: %s)",
            self.user,
            self.user.id if self.user else "?",
        )
        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Slash commands sincronizados en guild %s (%d): %s", GUILD_ID, len(synced), [c.name for c in synced])
            except Exception as e:
                logger.warning("Aviso sincronizando slash commands: %s", e)

'''
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(part1 + new_method + part2)
    print("PATCHED main.py SUCCESSFULLY")
else:
    print("Could not find index in main.py")

