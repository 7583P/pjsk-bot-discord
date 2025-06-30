import os
import re
import random
import asyncio
import datetime
import aiohttp                 # descarga catálogo canciones
import asyncpg                 # PostgreSQL asíncrono
import discord
from discord import Thread, TextChannel, MessageType
from discord.ext import commands, tasks   # loops periódicos
from discord import app_commands
# ———————————————————————————————————————————————
# IDs de los canales #join válidos
ALLOWED_JOIN_CHANNELS = {
    1371307353437110282,  # pjsk-queue → #join
    1378215330979254402,  # jp-pjsk   → #join
}

def is_allowed_leave(ch: discord.abc.GuildChannel) -> bool:
    # 1) Si es el canal #join
    if ch.id in ALLOWED_JOIN_CHANNELS:
        return True
    # 2) O si es un hilo cuyo padre es #join
    if isinstance(ch, Thread) and ch.parent_id in ALLOWED_JOIN_CHANNELS:
        return True
    return False
# ———————————————————————————————————————————————

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

BRACKET_RANGES = {
    "Placement": (28, 30),
    "Bronze":    (25, 27),
    "Gold":      (28, 30),
    "Diamond":   (31, 33),
}

def dynamic_range(counts: dict[str, bool]) -> tuple[int, int]:
    medias = []
    for rank, pres in counts.items():
        if pres:
            lo, hi = BRACKET_RANGES[rank]
            medias.append((lo + hi) / 2)
    if not medias:
        return BRACKET_RANGES["Placement"]
    promedio = sum(medias) / len(medias)
    centro   = round(promedio)
    lo_dyn, hi_dyn = centro - 1, centro + 1
    return max(25, lo_dyn), min(33, hi_dyn)



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
    def _range_for_counts(counts: dict[str, bool]) -> tuple[int, int]:
        # delegar en la función dinámica
        return dynamic_range(counts)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._songs_lock = asyncio.Lock()
        self.rooms: dict[int, dict] = {}
        self.inactivity: dict[int, dict[int, dict]] = {}
        self.monitor_inactivity.start()


    @tasks.loop(minutes=1)
    async def monitor_inactivity(self):
        now = datetime.datetime.utcnow()
        for rid, info in list(self.rooms.items()):
            thread  = info["thread"]
            players = info["players"]

            # Si la sala está llena, desactivar monitoreo
            if len(players) >= 5:
                self.inactivity.pop(thread.id, None)
                continue

            data = self.inactivity.setdefault(thread.id, {})
            for member in list(players):
                entry  = data.setdefault(member.id, {"last": now, "warned_at": None})
                last, warned = entry["last"], entry["warned_at"]

                # 5 min sin escribir → avisar
                if warned is None and now - last > datetime.timedelta(minutes=5):
                    await thread.send(
                        f"{member.mention} 5 minutess have passed, type something within 2 minutes to stay in the room"
                    )
                    entry["warned_at"] = now

                # 2 min después del aviso y sigue inactivo → expulsar
                elif warned and now - warned > datetime.timedelta(minutes=2) \
                     and now - last > datetime.timedelta(minutes=7):
                    try:
                        await thread.remove_user(member)
                    except:
                        pass
                    players.remove(member)
                    data.pop(member.id, None)
                    await thread.send(
                        f"{member.mention} have been kicked due to inactivity"
                    )

                    # — Si la sala ha quedado vacía (solo queda el bot), archivarla y borrarla —
                    if not players:
                        try:
                            # Archivamos y bloqueamos el hilo
                            await thread.edit(archived=True, locked=True)
                            # Lo borramos del servidor
                            await thread.delete()
                        except:
                            pass
                        # Limpiamos las estructuras internas
                        self.rooms.pop(rid, None)
                        self.inactivity.pop(thread.id, None)
                        # Pasamos a la siguiente sala
                        continue

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # — Reset de inactividad en hilos privados —
        if isinstance(message.channel, Thread) and not message.author.bot:
            now = datetime.datetime.utcnow()
            data = self.inactivity.setdefault(message.channel.id, {})
            entry = data.setdefault(
                message.author.id,
                {"last": now, "warned_at": None}
            )
            entry["last"] = now
            entry["warned_at"] = None

            # — Borra TODO mensaje con mención (usuarios, roles, everyone) —
            if message.mentions or message.role_mentions or message.mention_everyone:
                try:
                    await message.delete()
                except:
                    pass
                return

        # Ignorar mensajes del bot
        if message.author.bot:
            return

        # … el resto de tu lógica on_message …


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

    @app_commands.command(
        name="start",
        description="Inicia la sala y genera el poll (temporal)"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def start(self, interaction: discord.Interaction):
        # 1) Recoge los jugadores humanos en #join
        join_chan = discord.utils.get(
            interaction.guild.channels, name=JOIN_CHANNEL_NAME
        )
        players = [m for m in join_chan.members if not m.bot]

        # 2) Valida 2–5 jugadores
        if len(players) < 2 or len(players) > 5:
            return await interaction.response.send_message(
                "🔸 La sala debe tener entre 2 y 5 jugadores.",
                ephemeral=True
            )

        # 3) Construye el dict de rangos presentes
        counts = {r: False for r in BRACKET_RANGES}
        for m in players:
            roles = [role.name for role in m.roles]
            for r in counts:
                if r in roles:
                    counts[r] = True
                    break
            else:
                counts["Placement"] = True

        # 4) Calcula el rango dinámico
        lo, hi = self._range_for_counts(counts)

        # 5) Obtiene 5 canciones y crea hilo
        songs = await self._get_9_songs(lo, hi)
        picks = songs[:5]
        thread = await interaction.channel.create_thread(
            name=f"Sala {len(players)} ({lo}–{hi}★)",
            auto_archive_duration=60,
            type=discord.ChannelType.public_thread
        )

        text = f"🎶 **Poll (Expert {lo}–{hi}★)**\n"
        for i, (title, lvl, diff) in enumerate(picks, start=1):
            text += f"{i}️⃣ {title} – {lvl}★ ({diff.capitalize()})\n"

        poll_msg = await thread.send(text)
        for emoji in ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]:
            await poll_msg.add_reaction(emoji)

        # 6) Programa el cierre automático en 30 minutos
        async def close_after(delay, thr):
            await asyncio.sleep(delay)
            await thr.edit(archived=True)
        asyncio.create_task(close_after(30 * 60, thread))

        # 7) Confirma al invocador
        await interaction.response.send_message(
            f"✅ Sala iniciada en {thread.mention}. ¡A votar!",
            ephemeral=True
        )

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
                await info["thread"].edit(name=f"room-{idx}")
            except:
                pass
            new_rooms[idx] = info
        self.rooms = new_rooms

    @app_commands.command(name="c", description="Join a room")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def join_room(self, interaction: discord.Interaction):
        ch = interaction.channel
        # Solo en #join de pjsk-queue o jp-pjsk
        if ch.id not in ALLOWED_JOIN_CHANNELS:
            return await interaction.response.send_message(
                "This command only works on #join",
                ephemeral=True
            )

        # — Resto de tu lógica igual que antes —
        # Determinar la categoría padre (si viene de Thread)
        parent_chan = ch
        if isinstance(parent_chan, Thread) and parent_chan.parent:
            parent_chan = parent_chan.parent
        current_cat = parent_chan.category_id or 0

        member = interaction.user
        mmr_val, _ = await self.fetch_player(member.id)

        # Buscar sala en esta categoría
        best_rid = None
        for rid, info in self.rooms.items():
            if info["category_id"] == current_cat and len(info["players"]) < 5:
                best_rid = rid
                break

        # Crear sala si no hay
        if best_rid is None:
            new_id = max(self.rooms.keys(), default=0) + 1
            thread = await ch.create_thread(
                name=f"sala-{new_id}",
                auto_archive_duration=60,
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            self.rooms[new_id] = {
                "players": [], "thread": thread,
                "category_id": current_cat,
            }
            best_rid = new_id

            # Borrar aviso automático
            async for msg in ch.history(limit=5):
                if msg.type == MessageType.thread_created and msg.author == interaction.user:
                    await msg.delete()
                    break

            self.bot.dispatch('room_updated', best_rid)

        # Añadir jugador
        room = self.rooms[best_rid]
        if member in room["players"]:
            return await interaction.response.send_message(
                "You are already in a room", ephemeral=True
            )
        room["players"].append(member)

        await interaction.response.send_message(
            f"Joined room{best_rid}.", ephemeral=True
        )
        await ch.send(f"**{member.display_name}** Joined room {best_rid} (MMR {mmr_val})")
        await room["thread"].add_user(member)

        await self.sort_and_rename_rooms(interaction.guild)
        if len(room["players"]) == 5:
            asyncio.create_task(self.launch_song_poll(room))


    @app_commands.command(name="d", description="Salir de la sala")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def leave_room(self, interaction: discord.Interaction):
        ch = interaction.channel

        # — Guard: solo en #join o sus hilos —
        if not is_allowed_leave(ch):
            return await interaction.response.send_message(
                "This command only works on #join or in room thread",
                ephemeral=True
            )

        member = interaction.user

        # Buscamos al usuario en las salas de esta categoría
        for rid, info in list(self.rooms.items()):
            # Solo salas cuyo thread pertenezca a este join
            if info["thread"].parent_id not in ALLOWED_JOIN_CHANNELS:
                continue

            if member in info["players"]:
                # Quitar del thread y de la lista
                info["players"].remove(member)
                await info["thread"].send(f"**{member.display_name}** Leaved")
                try:
                    await info["thread"].remove_user(member)
                except:
                    pass

                # Si la sala quedó vacía, arquivar y borrar
                if not info["players"]:
                    try:
                        await info["thread"].edit(archived=True, locked=True)
                        await info["thread"].delete()
                    except:
                        pass
                    self.rooms.pop(rid)
                    self.bot.dispatch('room_finished', rid)

                # Confirmación al usuario y reordenar
                await interaction.response.send_message(
                    f"Leaved room {rid}.", ephemeral=True
                )
                await self.sort_and_rename_rooms(interaction.guild)
                return

        # Si no lo encontramos en ninguna sala válida
        await interaction.response.send_message(
            "You are not in a room", ephemeral=True
        )



    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="mmr", description="Muestra tu MMR actual")
    async def mmr_self(self, interaction: discord.Interaction):
        mmr_val, role = await self.fetch_player(interaction.user.id)
        name = interaction.user.display_name
        if role == "Placement":
            await interaction.response.send_message(
                f"{name} Is in placement"
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
                f"{name} has {mmr_val} MMR and rank {role}."
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
            return await ctx.send("Use this command into a room thread")

        players_list = []
        for ln in lines:
            m = ENTRY_RE.match(ln)
            if not m:
                return await ctx.send(f"❌ Formato incorrecto: {ln}")
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