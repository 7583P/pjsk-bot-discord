import os
import re
import random
import asyncio

import aiohttp                 # descarga catálogo canciones
import asyncpg                 # PostgreSQL asíncrono
import discord
from discord.ext import commands, tasks   # loops periódicos
from discord import app_commands

DB_PATH           = "matchmaking.db"   # ignorado, pero por compatibilidad
GUILD_ID          = int(os.getenv("GUILD_ID", "0"))
JOIN_CHANNEL_NAME = "join"
DATABASE_URL      = os.getenv("DB_HOST")  # postgresql://user:pass@host:port/dbname

ENTRY_RE = re.compile(
    r"^<@!?(?P<id>\d+)>\s*\[(?P<cc>\w{2})\]\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$"
)

def country_flag(code: str) -> str:
    code = code.upper()
    if len(code) != 2:
        return ""
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


class SongPollView(discord.ui.View):
    """Select de 9 canciones que abre votación de 1 minuto."""
    def __init__(self, songs, *, thread, timeout=60):
        super().__init__(timeout=timeout)
        self.thread = thread
        self.message: discord.Message
        self.vote_map: dict[int,str] = {}
        self.votes:    dict[str,int] = {}

        options = [
            discord.SelectOption(
                label=f"{title} (Lv {level} {diff})",
                value=f"{title}|{level}|{diff}"
            )
            for title, level, diff in songs
        ]

        select = discord.ui.Select(
            placeholder="👆 Tu voto (puedes cambiar antes de 1 min)",
            options=options,
            min_values=1,
            max_values=1
        )

        async def select_callback(inter: discord.Interaction):
            uid    = inter.user.id
            choice = select.values[0]
            if uid in self.vote_map:
                prev = self.vote_map[uid]
                self.votes[prev] -= 1
            self.vote_map[uid] = choice
            self.votes[choice] = self.votes.get(choice, 0) + 1
            await inter.response.send_message(
                f"✅ Tu voto por **{choice.split('|')[0]}** ha sido registrado.",
                ephemeral=True
            )

        select.callback = select_callback
        self.add_item(select)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(view=self)
        except:
            pass

        if not self.votes:
            return await self.thread.send(
                "⚠️ No se registraron votos en el tiempo establecido."
            )

        max_votes = max(self.votes.values())
        winners   = [opt for opt,c in self.votes.items() if c == max_votes]
        chosen    = random.choice(winners) if len(winners)>1 else winners[0]
        title, lvl, diff = chosen.split("|")

        await self.thread.send(
            f"🏆 **Resultado de la votación** 🏆\n"
            f"La canción ganadora es **{title}** (Lv {lvl}, {diff}) con **{max_votes} votos**."
        )
        self.stop()


class Matchmaking(commands.Cog):
    RAW_EN = (
        "https://raw.githubusercontent.com/"
        "Sekai-World/sekai-master-db-en-diff/main"
    )
    DIFFS = ("append", "master", "expert")  # prioridad de dificultad

    @staticmethod
    def _range_for_counts(c):
        if c["Diamond"]:
            return (31, 33)
        if c["Gold"]:
            return (28, 30)
        if c["Bronze"]:
            return (25, 27)
        return (28, 30)  # Placement o mixto

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._songs_lock = asyncio.Lock()
        self.rooms: dict[int, dict] = {}

    async def cog_load(self):
        # Pool global de PostgreSQL
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    mmr     INTEGER DEFAULT 0,
                    role    TEXT DEFAULT 'Placement'
                );
                """
            )
        # arranca el loop que refresca las canciones
        self.refresh_songs.start()
        await self.refresh_songs()

    async def cog_unload(self):
        await self.db_pool.close()

    @tasks.loop(hours=6)
    async def refresh_songs(self):
        async with self._songs_lock:
            async with aiohttp.ClientSession() as s:
                async def grab(name):
                    async with s.get(f"{self.RAW_EN}/{name}.json") as r:
                        return await r.json(content_type=None)

                musics = await grab("musics")
                diffs  = await grab("musicDifficulties")

            title = {m["id"]: m["title"] for m in musics}
            rows = [
                (d["musicId"], title[d["musicId"]],
                 d["musicDifficulty"], d["playLevel"])
                for d in diffs if d["musicDifficulty"] in self.DIFFS
            ]
            async with self.db_pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS songs")
                await conn.execute(
                    """CREATE TABLE songs(
                         id INTEGER, title TEXT, diff TEXT, level INTEGER)"""
                )
                await conn.executemany(
                    "INSERT INTO songs VALUES($1, $2, $3, $4)", rows
                )
        print("[songs] Catálogo actualizado")

    @refresh_songs.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _get_9_songs(self, low, high):
        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""SELECT title, level, diff FROM songs
                        WHERE level BETWEEN $1 AND $2 
                        AND diff IN ({','.join(f'${i+3}' for i in range(len(self.DIFFS)))})""",
                    low, high, *self.DIFFS
                )
        by_lvl = {lvl: [] for lvl in range(high, low - 1, -1)}
        for r in rows:
            t, lvl, diff = r["title"], r["level"], r["diff"]
            by_lvl[lvl].append((t, lvl, diff.capitalize()))

        picks = []
        for lvl in range(high, low - 1, -1):  # 30→29→28
            random.shuffle(by_lvl[lvl])
            picks.extend(by_lvl[lvl][:3])
        return picks[:9]

    async def launch_song_poll(self, room_info):
        thread  = room_info["thread"]
        players = room_info["players"]
        counts = {"Placement": 0, "Bronze": 0, "Gold": 0, "Diamond": 0}
        for m in players:
            for r in counts:
                if discord.utils.get(m.roles, name=r):
                    counts[r] += 1
                    break
        low, high = self._range_for_counts(counts)
        songs = await self._get_9_songs(low, high)

        if not songs:
            return await thread.send("⚠️ No hay canciones en ese rango.")

        view = SongPollView(songs, thread=thread, timeout=60)
        msg  = await thread.send(
            f"🎉 Sala completa · Niveles {high}-{low}\n"
            "Tienen **1 minuto** para votar la canción:",
            view=view
        )
        view.message = msg

    async def fetch_player(self, user_id: int):
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT mmr, role FROM players WHERE user_id = $1", user_id
            )
            if row:
                return row["mmr"], row["role"]
            await conn.execute(
                "INSERT INTO players (user_id, mmr, role) VALUES ($1, 0, 'Placement')",
                user_id,
            )
            row = await conn.fetchrow(
                "SELECT mmr, role FROM players WHERE user_id = $1", user_id
            )
            return row["mmr"], row["role"]

    async def ensure_join_channel(self, guild: discord.Guild):
        for ch in guild.text_channels:
            if ch.name == JOIN_CHANNEL_NAME:
                return ch
        return await guild.create_text_channel(JOIN_CHANNEL_NAME)

    async def sort_and_rename_rooms(self, guild: discord.Guild):
        avgs = []
        for rid, info in self.rooms.items():
            members = info["players"]
            if not members:
                continue
            total = 0
            for m in members:
                m_mmr, _ = await self.fetch_player(m.id)
                total += m_mmr
            avgs.append((rid, total / len(members)))
        avgs.sort(key=lambda x: x[1], reverse=True)
        new_rooms = {}
        for idx, (old_rid, _) in enumerate(avgs, start=1):
            info = self.rooms[old_rid]
            try:
                await info["thread"].edit(name=f"sala-{idx}")
            except:
                pass
            new_rooms[idx] = info
        self.rooms = new_rooms

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="c", description="Unirse a una sala")
    async def join_room(self, interaction: discord.Interaction):
        if interaction.channel.name != JOIN_CHANNEL_NAME:
            return await interaction.response.send_message(
                f"❌ Este comando solo funciona en el canal #{JOIN_CHANNEL_NAME}.",
                ephemeral=True
            )
        member = interaction.user
        mmr_val, _ = await self.fetch_player(member.id)

        best_rid, best_score = None, float("inf")
        for rid, info in self.rooms.items():
            if len(info["players"]) >= 5:
                continue
            vals = []
            for m in info["players"]:
                m_mmr, _ = await self.fetch_player(m.id)
                vals.append(m_mmr)
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            diff = abs(avg - mmr_val)
            if diff < best_score:
                best_rid, best_score = rid, diff
        if best_rid is None:
            new_id = max(self.rooms.keys(), default=0) + 1
            ch = interaction.channel
            thread = await ch.create_thread(
                name=f"sala-{new_id}",
                auto_archive_duration=60,
                type=discord.ChannelType.public_thread,
            )
            self.rooms[new_id] = {"players": [], "thread": thread}
            best_rid = new_id
        room = self.rooms[best_rid]
        if member in room["players"]:
            return await interaction.response.send_message(
                "❌ Ya estás en esa sala.", ephemeral=True
            )
        room["players"].append(member)
        await interaction.response.send_message(
            f"✅ Te uniste a sala-{best_rid} — MMR {mmr_val}"
        )
        await room["thread"].add_user(member)
        await room["thread"].send(f"{member.display_name} se unió — MMR {mmr_val}")
        await self.sort_and_rename_rooms(interaction.guild)
        if len(room["players"]) == 5:
            asyncio.create_task(self.launch_song_poll(room))

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="d", description="Salir de la sala")
    async def leave_room(self, interaction: discord.Interaction):
        member = interaction.user
        for rid, info in list(self.rooms.items()):
            if member in info["players"]:
                info["players"].remove(member)
                await info["thread"].send(f"{member.display_name} salió.")
                await info["thread"].remove_user(member)
                if not info["players"]:
                    try:
                        await info["thread"].edit(archived=True, locked=True)
                    except:
                        pass
                    self.rooms.pop(rid)
                await interaction.response.send_message(
                    f"🚪 Saliste de sala-{rid}."
                )
                await self.sort_and_rename_rooms(interaction.guild)
                return
        await interaction.response.send_message(
            "❌ No estás en ninguna sala.", ephemeral=True
        )

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="mmr", description="Muestra tu MMR actual")
    async def mmr_self(self, interaction: discord.Interaction):
        mmr_val, role = await self.fetch_player(interaction.user.id)
        name = interaction.user.display_name
        if role == "Placement":
            await interaction.response.send_message(
                f"{name} está en estado Placement y aún no tiene MMR asignado."
            )
        else:
            await interaction.response.send_message(
                f"{name} tiene {mmr_val} MMR y rango {role}."
            )

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="mmr_user", description="Muestra el MMR de otro jugador")
    async def mmr_user(self, interaction: discord.Interaction, user: discord.Member):
        mmr_val, role = await self.fetch_player(user.id)
        name = user.display_name
        if role == "Placement":
            await interaction.response.send_message(
                f"{name} Is in placement"
            )
        else:
            await interaction.response.send_message(
                f"{name} tiene {mmr_val} MMR y rango {role}."
            )

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="top10", description="Top 10 jugadores por MMR")
    async def top10_slash(self, interaction: discord.Interaction):
        async with self.db_pool.acquire() as conn:
            top = await conn.fetch(
                "SELECT user_id, mmr FROM players ORDER BY mmr DESC LIMIT 10"
            )
        if not top:
            return await interaction.response.send_message(
                "Aún no hay jugadores con MMR definido."
            )
        lines = []
        for i, row in enumerate(top, 1):
            uid, mmr_val = row["user_id"], row["mmr"]
            mem = interaction.guild.get_member(uid)
            nm = mem.display_name if mem else f"ID {uid}"
            lines.append(f"{i}. {nm} — {mmr_val} MMR")
        await interaction.response.send_message("\n".join(lines))

    @commands.command(name="debug_diffs")
    async def debug_diffs(self, ctx: commands.Context):
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT diff FROM songs")
        diffs = [r["diff"] for r in rows]
        await ctx.send(f"Dificultades en la tabla songs: {diffs}")

    @commands.command(name="debug_poll")
    async def debug_poll(self, ctx: commands.Context):
        songs = await self._get_9_songs(1, 99)
        await ctx.send(f"🎵 debug_poll sacó {len(songs)} canciones", ephemeral=True)
        if not songs:
            return await ctx.send("⚠️ No hay canciones en debug_poll.", ephemeral=True)
        view = SongPollView(songs, thread=ctx.channel, timeout=60)
        msg  = await ctx.send(
            "🎵 Poll de prueba (60s para votar):",
            view=view
        )
        view.message = msg

    @commands.command(name="submit5")
    async def submit5(self, ctx: commands.Context, *, block: str):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) != 5:
            return await ctx.send(
                "⚠️ Debes enviar 5 líneas: @user [CC] p,g,gd,b,m"
            )
        room = next(
            (r for r in self.rooms.values() if r["thread"].id == ctx.channel.id),
            None,
        )
        if not room:
            return await ctx.send("❌ Usa este comando dentro de un hilo de sala.")

        players_list = []
        for ln in lines:
            m = ENTRY_RE.match(ln)
            if not m:
                return await ctx.send(f"❌ Formato incorrecto: `{ln}`")
            uid = int(m.group("id"))
            cc = m.group("cc")
            stats = list(map(int, m.group("stats").split(",")))
            member = ctx.guild.get_member(uid) or await ctx.guild.fetch_member(uid)

            weights = [5, 3, 2, 1, 0]
            total = sum(s * w for s, w in zip(stats, weights))
            old, db_role = await self.fetch_player(uid)

            placement_bonus = None
            bonus = old
            async with self.db_pool.acquire() as conn:
                if old == 0 and db_role == "Placement":
                    non_perfect = sum(stats[1:])
                    if non_perfect <= 15:
                        bonus = 2000
                        placement_bonus = "(+2000 Placement Diamond)"
                    elif non_perfect <= 50:
                        bonus = 1000
                        placement_bonus = "(+1000 Placement Gold)"
                    else:
                        bonus = 0
                        placement_bonus = "(+0 Placement Bronze)"
                    await conn.execute(
                        "UPDATE players SET mmr=$1 WHERE user_id=$2", bonus, uid
                    )

            players_list.append(
                {
                    "member": member,
                    "country": cc,
                    "total": total,
                    "old": bonus,
                    "placement_bonus": placement_bonus,
                }
            )

        players_list.sort(key=lambda x: x["total"], reverse=True)
        avg = sum(p["old"] for p in players_list) / 5
        unit = max(1, int(avg // 10))

        results = []
        for idx, p in enumerate(players_list, 1):
            mu = {1: 3, 2: 2, 3: 0.5, 4: -1, 5: -2}[idx]
            delta = int(mu * unit)
            new = p["old"] + delta
            role_name = (
                "Bronze" if new < 1000 else "Gold" if new < 2000 else "Diamond"
            )
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE players SET mmr=$1,role=$2 WHERE user_id=$3",
                    new, role_name, p["member"].id,
                )
            role_obj = discord.utils.get(ctx.guild.roles, name=role_name)
            if role_obj:
                await p["member"].remove_roles(
                    *[r for r in p["member"].roles if r.name in {"Bronze", "Gold", "Diamond"}]
                )
                await p["member"].add_roles(role_obj)

            results.append(
                {
                    "idx": idx,
                    "member": p["member"],
                    "country": p["country"],
                    "total": p["total"],
                    "old": p["old"],
                    "delta": delta,
                    "new": new,
                    "bonus": p["placement_bonus"],
                }
            )

        lines_out = ["🏆 Resultados de ¡submit5! 🏆\n"]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for r in results:
            if r["bonus"]:
                lines_out.append(r["bonus"])
            med = medals.get(r["idx"], "")
            flag = country_flag(r["country"])
            sign = "+" if r["delta"] >= 0 else ""
            lines_out.append(f"{med} {flag} {r['member'].mention}")
            lines_out.append(f"• Puntos Totales: {r['total']}")
            lines_out.append(f"• MMR: {r['old']}{sign}{r['delta']} = {r['new']}\n")
        await ctx.send("\n".join(lines_out))


async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))