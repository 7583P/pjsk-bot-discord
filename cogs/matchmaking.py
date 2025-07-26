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
from math import floor, ceil
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

# — Nuevos intervalos de nivel para cada rango —
BRACKET_RANGES = {
    "Placement":     (23, 28),
    "Iron":          (17, 22),
    "Bronze":        (19, 24),
    "Silver":        (20, 25),
    "Gold":          (22, 27),
    "Platinum":      (23, 28),
    "Diamond":       (27, 30),
    "Crystal":       (28, 31),
    "Master":        (29, 33),
    "Champion":      (30, 34),
    "GrandChampion": (31, 35),
    "Legend":        (32, 37),
}


from math import floor, ceil

def dynamic_range(counts: dict[str, int]) -> tuple[int, int]:
    centers: list[int] = []
    gaps:    set[int] = set()

    # 1) Recolectar centros y gaps de cada rango
    for rank, num in counts.items():
        if num <= 0: continue
        lo_r, hi_r = BRACKET_RANGES[rank]
        gaps.add(hi_r - lo_r)
        centers.extend([round((lo_r + hi_r) / 2)] * num)

    # 2) Fallback a Placement si no hay jugadores
    if not centers:
        return BRACKET_RANGES["Placement"]

    # 3) Centro promedio redondeado
    avg_center = round(sum(centers) / len(centers))

    # 4) Elegir gap: si todos iguales (3 o 4), lo usamos; si mezclados → 5
    gap = gaps.pop() if len(gaps) == 1 else 5

    # 5) Construir intervalo centrado en avg_center con ese gap
    lo = avg_center - floor(gap / 2)
    hi = avg_center + ceil(gap / 2)
    return lo, hi



def get_rank_from_mmr(mmr: int) -> str:

    if mmr <= 100:
        return "Iron"
    elif mmr <= 200:
        return "Bronze"
    elif mmr <= 300:
        return "Silver"
    elif mmr <= 400:
        return "Gold"
    elif mmr <= 500:
        return "Platinum"
    elif mmr <= 600:
        return "Diamond"
    elif mmr <= 700:
        return "Crystal"
    elif mmr <= 800:
        return "Master"
    elif mmr <= 900:
        return "Champion"
    elif mmr <= 999:
        return "Grand Champion"
    else:
        return "Legend"

RANK_ROLE_IDS = {
    "Iron":        1394444407536881845,        
    "Bronze":        1371324225838645339,
    "Silver":        1389343997100560514,
    "Gold":          1371324328708149328,
    "Platinum":      1389343805521789098,
    "Diamond":      1371324561542484108,
    "Crystal":       1394445724703527012,
    "Master":       1394635883466199110,
    "Champion":      1371323543501144115,
    "Grand Champion": 1394444744892874832,
    "Legend":        1371323380510363749,
}

# ——————————————————————————————
# Constantes de Placement
# ——————————————————————————————
PLACEMENT_ROLE_ID = 1371321594068336811
PLACEMENT_MMR_BONUS = {
    "Iron":     50,
    "Bronze":   101,
    "Silver":   201,
    "Gold":     301,
    "Platinum": 401,
    "Diamond":  501,
}



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

    async def mostrar_canciones_aleatorias(self, thread, low: int, high: int):
        """
        Envía 5 canciones al hilo, con gap low–high, en orden aleatorio.
        """
        canciones = await self._get_9_songs(low, high)
        random.shuffle(canciones)
        lineas = [
            f"{i+1}. {titulo} (Lv {nivel}) ({diff.capitalize()})"
            for i, (titulo, nivel, diff) in enumerate(canciones[:5], start=1)
        ]
        texto = "🎶 Canciones sugeridas 🎶\n" + "\n".join(lineas)
        await thread.send(texto)


    @app_commands.command(
        name="start",
        description="Inicia la sugerencia de 5 canciones según el rango de los jugadores"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def start(self, interaction: discord.Interaction):
        ch = interaction.channel

        # 1) Solo en #join o sus hilos
        is_join_chan   = ch.id in ALLOWED_JOIN_CHANNELS
        is_join_thread = isinstance(ch, Thread) and ch.parent_id in ALLOWED_JOIN_CHANNELS
        if not (is_join_chan or is_join_thread):
            return await interaction.response.send_message(
                "This command only works in rooms",
                ephemeral=True
            )

        # 2) Canal padre y lista de players
        if is_join_thread:
            join_chan  = ch.parent
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

        # 3) Validar 2–5 jugadores
        if not (2 <= len(players) <= 5):
            return await interaction.response.send_message(
                "🔸 Room must have between 2-5 players",
                ephemeral=True
            )

        # 4) Contar rangos
        counts = { rank: 0 for rank in BRACKET_RANGES }
        for m in players:
            user_rank = next(
                (r for r in BRACKET_RANGES if discord.utils.get(m.roles, name=r)),
                "Placement"
            )
            counts[user_rank] += 1

        # 5) Calcular intervalo low/high
        lo, hi = dynamic_range(counts)

        # 6) Crear o reutilizar hilo
        if is_join_thread:
            thread = ch
        else:
            new_id = max(self.rooms.keys(), default=0) + 1
            thread = await join_chan.create_thread(
                name=f"Sala {new_id} ({lo}–{hi}★)",
                auto_archive_duration=60,
                type=discord.ChannelType.public_thread
            )
            self.rooms[new_id] = {
                "players": players.copy(),
                "thread": thread,
                "category_id": join_chan.category_id or 0,
                "closed": True,
            }

        # 7) Enviar 5 canciones aleatorias
        await self.mostrar_canciones_aleatorias(thread, lo, hi)

        # 8) Confirmación
        await interaction.response.send_message(
            f"✅ Sugerencias enviadas en {thread.mention}",
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
            # Calcula low/hi y manda 5 canciones de una
            counts = {r:0 for r in BRACKET_RANGES}
            for m in room["players"]:
                ru = next((r for r in BRACKET_RANGES if discord.utils.get(m.roles, name=r)), "Placement")
                counts[ru] += 1
            lo, hi = dynamic_range(counts)
            await self.mostrar_canciones_aleatorias(room["thread"], lo, hi)



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

    def get_role_from_notes(stats):
        # stats: [perfect, great, good, bad, miss]
        total_notes = stats[1] + stats[2] + stats[3] + stats[4]  # Suma solo desde great en adelante
        if 0 <= total_notes <= 5:
            return "Diamond"
        elif 6 <= total_notes <= 15:
            return "Platinum"
        elif 16 <= total_notes <= 50:
            return "Gold"
        elif 51 <= total_notes <= 100:
            return "Silver"
        elif 101 <= total_notes <= 250:
            return "Bronze"
        else:
            return "Iron"


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

        # ——————————————————————————————
        # FASE 1: bonus fijo para Placement
        # ——————————————————————————————
        for p in players_list:
            if p["role"] == "Placement":
                bonus_role      = get_role_from_notes(p["stats"])
                p["mmr_actual"] = PLACEMENT_MMR_BONUS[bonus_role]
            else:
                p["mmr_actual"] = p["old"]

        # ——————————————————————————————
        # FASE 2: delta posicional escalado con underdog/favourite adjustment
        # ——————————————————————————————
        # Ordenar por puntos (total), mayor a menor
        players_list.sort(key=lambda x: x["total"], reverse=True)

        avg  = sum(p["mmr_actual"] for p in players_list) / n
        unit = max(1, int(avg // 20))

        if   n == 5:
            mu_map = {1:1.5, 2:1.0, 3:0.5, 4:-1.0, 5:-1.5}
        elif n == 4:
            mu_map = {1:1.5, 2:0.75, 3:-0.5, 4:-1.5}
        elif n == 3:
            mu_map = {1:1.0, 2:0.0, 3:-1.0}
        elif n == 2:
            mu_map = {1:0.75, 2:-0.75}
        else:
            raise RuntimeError(f"Sala inválida: espera 2–5 jugadores, no {n}")

        summary = []
        for idx, p in enumerate(players_list, start=1):
            mmr_prev = p["mmr_actual"]
            base_raw = mu_map[idx] * unit
            base     = max(-39, min(39, int(base_raw)))

            rel = (mmr_prev - avg) / avg

            if idx == 1:
                # Ganador: underdog bonus / favorito nerf
                adj   = max(-0.5, min(0.5, -rel))
                scale = 1 + adj
            elif idx == n:
                # Perdedor: underdog penaliza menos / favorito penaliza más
                adj   = max(-0.5, min(0.5, rel))
                scale = 1 + adj
            else:
                # Intermedios: comportamiento neutro
                scale = 1 - min(abs(rel), 0.5)

            mmr_delta = int(base * scale)
            mmr_final = mmr_prev + mmr_delta
            role_name = get_rank_from_mmr(mmr_final)

            # Actualiza en BD
            await self.db_pool.execute(
                "UPDATE players SET mmr=$1, role=$2 WHERE user_id=$3",
                mmr_final, role_name, p["member"].id
            )
            # Actualiza roles en Discord
            role_id = RANK_ROLE_IDS.get(role_name)
            if role_id:
                role_obj   = ctx.guild.get_role(role_id)
                old_ranks  = set(RANK_ROLE_IDS.values()) | {PLACEMENT_ROLE_ID}
                keep_roles = [r for r in p["member"].roles if r.id not in old_ranks]
                await p["member"].edit(roles=keep_roles + [role_obj])
            # Actualiza nickname
            await p["member"].edit(nick=f"{p['member'].display_name} [{role_name}]")

            # Añade a summary para la tabla
            med = medals.get(idx, str(idx))
            summary.append((
                med,
                p["member"].display_name,
                p["total"],
                p["stats"],
                mmr_prev,
                mmr_delta,
                mmr_final
            ))

        join_parent    = ctx.channel.parent
        result_chan_id = JOIN_TO_RESULTS.get(join_parent.id)
        result_chan    = self.bot.get_channel(result_chan_id) if result_chan_id else ctx.channel

        table = "**🏆 Posiciones finales 🏆**\n"
        table += "Pos · Player · Points · PGGBM ·  MMR (previo + Δ = final)\n"
        for med, name, pts, stats, mmr_prev, mmr_delta, mmr_final in summary:
            pggbm = f"({stats[0]},{stats[1]},{stats[2]},{stats[3]},{stats[4]})"
            table += f"{med} · **{name}** · {pts} · {pggbm} · {mmr_prev} {mmr_delta:+d} = {mmr_final}\n"
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

    # Parseo de líneas y cálculo de 'total'
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    ENTRY_RE = re.compile(
        r"^<@!?(?P<id>\d+)>\s*\[(?P<cc>\w{2})\]\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$"
    )
    players_list = []
    for ln in lines:
        m = ENTRY_RE.match(ln)
        if not m:
            return await ctx.send(f"❌ Línea inválida: `{ln}`")
        uid   = int(m.group("id"))
        cc    = m.group("cc")
        stats = list(map(int, m.group("stats").split(",")))
        old, role = await self.fetch_player(uid)
        total = sum(s*w for s, w in zip(stats, [5,3,2,1,0]))
        players_list.append({
            "uid":   uid,
            "cc":    cc,
            "stats": stats,
            "old":   old,
            "role":  role,
            "total": total
        })

    n = len(players_list)
    if not (2 <= n <= 5):
        return await ctx.send("Room must have between 2-5 players")

    # Ordenar por total (descendente)
    players_list.sort(key=lambda x: x["total"], reverse=True)

    # ——————————————————————————————
    # FASE 1: bonus fijo para Placement
    # ——————————————————————————————
    for p in players_list:
        if p["role"] == "Placement":
            bonus_role      = get_role_from_notes(p["stats"])
            p["mmr_actual"] = PLACEMENT_MMR_BONUS[bonus_role]
        else:
            p["mmr_actual"] = p["old"]

    # ——————————————————————————————
    # FASE 2: delta posicional escalado con underdog/favourite adjustment
    # ——————————————————————————————
    avg  = sum(p["mmr_actual"] for p in players_list) / n
    unit = max(1, int(avg // 20))

    if   n == 5:
        mu_map = {1:1.5, 2:1.0, 3:0.5, 4:-1.0, 5:-1.5}
    elif n == 4:
        mu_map = {1:1.5, 2:0.75, 3:-0.5, 4:-1.5}
    elif n == 3:
        mu_map = {1:1.0, 2:0.0, 3:-1.0}
    elif n == 2:
        mu_map = {1:0.75, 2:-0.75}
    else:
        raise RuntimeError(f"Sala inválida: espera 2–5 jugadores, no {n}")

    summary = []
    medals = {1:"🥇", 2:"🥈", 3:"🥉"}
    old_ranks = set(RANK_ROLE_IDS.values()) | {PLACEMENT_ROLE_ID}
    for idx, p in enumerate(players_list, start=1):
        mmr_prev = p["mmr_actual"]
        base_raw = mu_map[idx] * unit
        base     = max(-39, min(39, int(base_raw)))

        rel = (mmr_prev - avg) / avg

        if idx == 1:
            adj   = max(-0.5, min(0.5, -rel))
            scale = 1 + adj
        elif idx == n:
            adj   = max(-0.5, min(0.5, rel))
            scale = 1 + adj
        else:
            scale = 1 - min(abs(rel), 0.5)

        mmr_delta = int(base * scale)
        mmr_final = mmr_prev + mmr_delta
        role_name = get_rank_from_mmr(mmr_final)

        # BD
        await self.db_pool.execute(
            "UPDATE players SET mmr=$1, role=$2 WHERE user_id=$3",
            mmr_final, role_name, p["uid"]
        )
        # Discord: rol y nickname
        member = ctx.guild.get_member(p["uid"])
        if member:
            if role_id := RANK_ROLE_IDS.get(role_name):
                keep = [r for r in member.roles if r.id not in old_ranks]
                await member.edit(roles=keep + [ctx.guild.get_role(role_id)])
            await member.edit(nick=f"{member.display_name} [{role_name}]")

        # Resumen para tabla
        med = medals.get(idx, str(idx))
        summary.append((
            med,
            member.display_name if member else f"<@{p['uid']}>",
            p["total"],
            p["stats"],
            mmr_prev,
            mmr_delta,
            mmr_final
        ))

    # Tabla resultado
    table = "**🏆 Posiciones finales (admin update) 🏆**\n"
    table += "Pos · Player · Points · PGGBM ·  MMR (previo + Δ = final)\n"
    for med, name, pts, stats, mmr_prev, mmr_delta, mmr_final in summary:
        pggbm = f"({stats[0]},{stats[1]},{stats[2]},{stats[3]},{stats[4]})"
        table += f"{med} · **{name}** · {pts} · {pggbm} · {mmr_prev} {mmr_delta:+d} = {mmr_final}\n"
    await ctx.send(table)



async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))

    # p