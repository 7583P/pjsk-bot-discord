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
# — IDs de los canales #join válidos —
ALLOWED_JOIN_CHANNELS = {
    1371307353437110282,  # pjsk-queue → #join
    1378215330979254402,  # jp-pjsk   → #join
}

# — Mapeo de canal #join → canal #results —
JOIN_TO_RESULTS = {
    1371307353437110282: 1371307931294892125,  # pjsk-queue → #results-pjsk
    1378215330979254402: 1388515450534494389,  # jp-pjsk   → #results-jp-pjsk
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
            # NUEVO BLOQUE AQUÍ:
            if info.get("started", False):
                self.inactivity.pop(thread.id, None)
                continue

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
                    # SOLO si la sala NO ha iniciado
                    if info.get("started", False):
                        continue
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
                            await thread.edit(archived=True, locked=True)
                            await thread.delete()
                        except:
                            pass
                        self.rooms.pop(rid, None)
                        self.inactivity.pop(thread.id, None)
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

        # 1) Calcula cuántos de cada rango había para definir low/high
        counts = {"Placement": 0, "Bronze": 0, "Gold": 0, "Diamond": 0}
        for m in players:
            for r in counts:
                if discord.utils.get(m.roles, name=r):
                    counts[r] += 1
                    break
            else:
                counts["Placement"] += 1

        low, high = dynamic_range(counts)

        # 2) Obtén el catálogo y selecciona solo 5 Expert
        all_songs = await self._get_9_songs(low, high)
        picks     = all_songs[:5]

        # ← Marcamos la sala como iniciada
        room_info["started"] = True

        # 3) Lanza la vista con esas 5 canciones
        view = SongPollView(picks, thread=thread, timeout=60)
        await thread.send(
            f"🎉 Sala completa · Niveles {high}★–{low}★ · Expert\n"
            "Tienen **1 minuto** para votar la canción:",
            view=view
        )


    @app_commands.command(
        name="start",
        description="Inicia la votación de 5 canciones (Expert)"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def start(self, interaction: discord.Interaction):
        ch = interaction.channel

        # — 1) Guardia: sólo #join o sus hilos —
        is_join_chan   = ch.id in ALLOWED_JOIN_CHANNELS
        is_join_thread = isinstance(ch, Thread) and ch.parent_id in ALLOWED_JOIN_CHANNELS
        if not (is_join_chan or is_join_thread):
            return await interaction.response.send_message(
                "This command only works in rooms",
                ephemeral=True
            )

        # — 2) Determinar canal padre y lista de players —
        if is_join_thread:
            join_chan = ch.parent
            # reutilizar sala existente
            room_entry = next(
                (r for r in self.rooms.values() if r["thread"].id == ch.id),
                None
            )
            if not room_entry:
                return await interaction.response.send_message(
                    "⚠️ No encuentro la sala asociada a este hilo.",
                    ephemeral=True
                )
            players = room_entry["players"]
        else:
            join_chan = ch
            players   = [m for m in join_chan.members if not m.bot]

        # — 3) Validar 2–5 jugadores —
        if not (2 <= len(players) <= 5):
            return await interaction.response.send_message(
                "🔸 Room must have betweem 2-5 players",
                ephemeral=True
            )

        # — 4) Calcular rango dinámico —
        counts = {r: False for r in BRACKET_RANGES}
        for m in players:
            for r in counts:
                if discord.utils.get(m.roles, name=r):
                    counts[r] = True
                    break
            else:
                counts["Placement"] = True
        lo, hi = dynamic_range(counts)

        # — 5) Crear y registrar hilo sólo si venimos de canal —
        if is_join_chan:
            thread = await join_chan.create_thread(
                name=f"Sala {len(self.rooms)+1} ({lo}–{hi}★)",
                auto_archive_duration=60,
                type=discord.ChannelType.public_thread
            )
            new_rid = max(self.rooms.keys(), default=0) + 1
            self.rooms[new_rid] = {
                "players": players.copy(),
                "thread": thread,
                "category_id": join_chan.category_id or 0,
                "closed": True,              # ← marcamos la sala como cerrada
            }
            self.bot.dispatch('room_updated', new_rid)
            # desactivar inactividad
            self.inactivity.pop(thread.id, None)
        else:
            # ya estaba en hilo: lo reutilizamos
            thread = ch
            # marcamos esa sala como cerrada también
            rid = next(r for r,info in self.rooms.items() if info["thread"].id == thread.id)
            self.rooms[rid]["closed"] = True    # ← closed aquí también
            self.inactivity.pop(thread.id, None)

        # — 6) Lanzar SongPollView con 5 canciones —
        all_songs = await self._get_9_songs(lo, hi)
        picks     = all_songs[:5]
        view      = SongPollView(picks, thread=thread, timeout=60)
        prompt    = f"🎶 **Votación (Expert {lo}–{hi}★)**\nTienen **1 minuto** para elegir su canción:"
        msg       = await thread.send(prompt, view=view)
        view.message = msg

        # — 7) Confirmación efímera —
        await interaction.response.send_message(
            f"✅ Votación iniciada en {thread.mention}",
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

        # Buscar sala NO cerrada en esta categoría
        best_rid = None
        for rid, info in self.rooms.items():
            if (
                info["category_id"] == current_cat
                and not info.get("closed", False)    # ← ignorar salas cerradas
                and len(info["players"]) < 5
            ):
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

def get_role_from_notes(total_notes):
    if total_notes <= 15:
        return "Diamond"
    elif total_notes <= 50:
        return "Gold"
    else:
        return "Bronze"

def get_role_from_mmr(mmr):
    if mmr <= 100:
        return "Bronze"
    elif mmr <= 500:
        return "Gold"
    else:
        return "Diamond"


    @commands.command(name="submit")
    async def submit(self, ctx: commands.Context, *, block: str):
        mm = self.bot.get_cog("Matchmaking")
        room = next((r for r in mm.rooms.values() if r["thread"].id == ctx.channel.id), None)
        if not room:
            return await ctx.send("Only works inside a room thread")

        if room.get("finished", False):
            return await ctx.send("The room has already been finished, cannot submit again.")

        players = room["players"]
        n = len(players)
        if not (2 <= n <= 5):
            return await ctx.send("Room must have between 2-5 players")

        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) != n:
            return await ctx.send(f"You should send {n} lines")

        intro = "✅ React ✅ to validate, ❎ to decline\n```\n" + "\n".join(lines) + "\n```"
        vote_msg = await ctx.send(intro)
        for emo in ("✅", "❎"):
            await vote_msg.add_reaction(emo)

        threshold = n // 2 + 1
        def check(reaction, user):
            return (
                reaction.message.id == vote_msg.id
                and not user.bot
                and str(reaction.emoji) in ("✅", "❎")
            )
        ganador = None
        try:
            while True:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", timeout=60.0, check=check
                )
                counts = {
                    emo: (discord.utils.get(vote_msg.reactions, emoji=emo).count - 1)
                    for emo in ("✅", "❎")
                }
                if counts["✅"] >= threshold:
                    ganador = "✅"
                    break
                if counts["❎"] >= threshold:
                    ganador = "❎"
                    break
        except asyncio.TimeoutError:
            counts = {
                emo: (discord.utils.get(vote_msg.reactions, emoji=emo).count - 1)
                for emo in ("✅", "❎")
            }
            if counts["✅"] > counts["❎"]:
                ganador = "✅"
            elif counts["❎"] > counts["✅"]:
                ganador = "❎"
            else:
                ganador = "Doubt"

        if ganador != "✅":
            return await ctx.send("There might be an error, try doing it again.")

        summary = []
        medals  = {1:"🥇",2:"🥈",3:"🥉"}
        ENTRY_RE = re.compile(r"^<@!?(?P<id>\d+)>\s*\[(?P<cc>\w{2})\]\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$")
        players_list = []
        for member, ln in zip(players, lines):
            m = ENTRY_RE.match(ln)
            if not m:
                return await ctx.send(f"Formato incorrecto para: {ln}")
            uid       = int(m.group("id"))
            cc        = m.group("cc")
            stats     = list(map(int, m.group("stats").split(",")))
            stats_str = m.group("stats")
            old, current_role = await self.fetch_player(uid)
            total  = sum(s*w for s, w in zip(stats, [5, 3, 2, 1, 0]))
            players_list.append({
                "member":    member,
                "cc":        cc,
                "total":     total,
                "old":       old,
                "role":      current_role,
                "stats":     stats,
                "stats_str": stats_str
            })

        players_list.sort(key=lambda x: x["total"], reverse=True)
        avg  = sum(p["old"] for p in players_list) / n
        unit = max(1, int(avg // 10))

        if n == 5:
            mu_map = {1: 3,   2: 2,    3: 0.5, 4: -1,   5: -2}
        elif n == 4:
            mu_map = {1: 2.5, 2: 1,    3: -0.5,4: -3}
        elif n == 3:
            mu_map = {1: 2,   2: 0,    3: -2}
        elif n == 2:
            mu_map = {1: 1.5, 2: -1.5}
        else:
            mu_map = {1: 1,   2: -1}

        summary = []
        for idx, p in enumerate(players_list, 1):
            current_mmr, current_role = await self.fetch_player(p["member"].id)
            total_notes = sum(p["stats"][1:])
            if current_role == "Placement":
                role_name = get_role_from_notes(total_notes)
                new = current_mmr
            else:
                raw_delta = int(mu_map.get(idx, 0) * unit)
                delta     = max(-39, min(39, raw_delta))
                new       = p["old"] + delta
                role_name = get_role_from_mmr(new)

            summary.append((
                medals.get(idx, ""),
                p["member"].display_name,
                p["stats_str"],
                p["total"],
                f"{p['old']}{'+' if new-p['old'] >= 0 else ''}{new-p['old']}"
            ))

            try:
                await self.db_pool.execute(
                    "UPDATE players SET mmr=$1,role=$2 WHERE user_id=$3",
                    new, role_name, p["member"].id
                )
            except Exception as e:
                print(f"[DB ERROR] Al actualizar MMR/rol: {e}")
            try:
                role_obj = discord.utils.get(ctx.guild.roles, name=role_name)
                if role_obj:
                    await p["member"].edit(roles=[r for r in p["member"].roles if r.name not in {"Bronze", "Gold", "Diamond"}] + [role_obj])
            except Exception as e:
                print(f"[DISCORD ERROR] Rol de {p['member'].display_name}: {e}")
            try:
                await p["member"].edit(nick=f"{p['member'].display_name} [{role_name}]")
            except Exception as e:
                print(f"[DISCORD ERROR] Al actualizar el nick: {e}")

        join_parent    = ctx.channel.parent
        result_chan_id = JOIN_TO_RESULTS.get(join_parent.id)
        result_chan    = self.bot.get_channel(result_chan_id) if result_chan_id else ctx.channel

        table = "**🏆 Posiciones finales 🏆**\n"
        table += "Pos · Player · Points · MMR Δ\n"
        for med, name, pts, mmr_delta in summary:
            table += f"{med} · **{name}** · {pts} · {mmr_delta}\n"
        await result_chan.send(table)
        await result_chan.send("MMR Updated")

        room["finished"] = True

        async def close_thread_later(thread, rooms, rid):
            await asyncio.sleep(120)
            try:
                await thread.edit(archived=True, locked=True)
                await thread.delete()
            except Exception:
                pass
            rooms.pop(rid, None)
        rid = next((rid for rid, r in mm.rooms.items() if r["thread"].id == ctx.channel.id), None)
        asyncio.create_task(close_thread_later(ctx.channel, mm.rooms, rid))


    @commands.command(name="update")
    async def update(self, ctx: commands.Context, *, block: str):
        if ctx.author.id != 878310498720940102:
            return await ctx.send("❌ Solo el administrador puede usar este comando.")

        lines = [l.strip() for l in block.split("\n") if l.strip()]
        ENTRY_RE = re.compile(r"^<@!?(?P<id>\d+)>\s*\[(?P<cc>\w{2})\]\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$")
        summary = []
        medals  = {1:"🥇",2:"🥈",3:"🥉"}

        players_list = []
        for idx, ln in enumerate(lines):
            m = ENTRY_RE.match(ln)
            if not m:
                summary.append(f"❌ Línea inválida: `{ln}`")
                continue
            uid    = int(m.group("id"))
            cc     = m.group("cc")
            stats  = list(map(int, m.group("stats").split(",")))
            old, _ = await self.fetch_player(uid)
            total  = sum(s*w for s,w in zip(stats, [5,3,2,1,0]))
            players_list.append({"member": ctx.guild.get_member(uid), "cc": cc, "total": total, "old": old, "uid": uid})

        players_list.sort(key=lambda x: x["total"], reverse=True)
        n = len(players_list)
        avg  = sum(p["old"] for p in players_list) / n if n > 0 else 0
        unit = max(1, int(avg // 10)) if n > 0 else 1

        for idx, p in enumerate(players_list, 1):
            mu = {1:3,2:2,3:0.5,4:-1,5:-2}[idx]
            delta = int(mu * unit)
            new = p["old"] + delta
            role_name = "Bronze" if new < 1000 else "Gold" if new < 2000 else "Diamond"
            try:
                await self.db_pool.execute(
                    "UPDATE players SET mmr=$1,role=$2 WHERE user_id=$3",
                    new, role_name, p["uid"]
                )
            except Exception as e:
                summary.append(f"❌ Error DB <@{p['uid']}>: {e}")

            try:
                member = p["member"]
                if member:
                    role_obj = discord.utils.get(ctx.guild.roles, name=role_name)
                    if role_obj:
                        await member.edit(roles=[r for r in member.roles if r.name not in {"Bronze", "Gold", "Diamond"}] + [role_obj])
                    await member.edit(nick=f"{member.display_name} [{role_name}]")
            except Exception as e:
                summary.append(f"❌ Error Discord <@{p['uid']}>: {e}")

            summary.append(f"{medals.get(idx,'')} <@{p['uid']}> → {new} MMR, rango {role_name}")

        await ctx.send("\n".join(summary))


async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))

    # p