import os
import re
import random
import asyncio
import datetime
import aiohttp                 # descarga catálogo canciones
import asyncpg                 # PostgreSQL asíncrono
import discord
from discord import app_commands, Embed
from discord import ui, ButtonStyle
from discord import Thread, TextChannel, MessageType
from discord.ext import commands, tasks   # loops periódicos
from discord.ui import View, button
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

# Prioridad global de dificultades, ahora incluyendo hard
DIFFICULTY_PRIORITY = ("hard", "expert", "master", "append")

# Mapeo de rangos a dificultades permitidas
ALLOWED_DIFFS_BY_RANK = {
    "Placement":     ("hard", "expert", "master", "append"),
    "Iron":          ("hard", "expert", "master"),
    "Bronze":        ("hard", "expert", "master", "append"),
    "Silver":        ("hard", "expert", "master", "append"),
    "Gold":          ("hard", "expert", "master", "append"),
    "Platinum":      ("hard", "expert", "master", "append"),
    "Diamond":       ("expert", "master", "append"),
    "Crystal":       ("expert", "master", "append"),
    "Master":        ("expert", "master", "append"),
    "Champion":      ("expert", "master", "append"),
    "Grand Champion": ("expert", "master", "append"),
    "Legend":        ("master", "append"),
}

# Nuevos rangos con gap exacto
BRACKET_RANGES = {
    "Placement":     (21, 28),
    "Iron":          (17, 22),
    "Bronze":        (19, 24),
    "Silver":        (20, 25),
    "Gold":          (22, 27),
    "Platinum":      (23, 28),
    "Diamond":       (25, 30),
    "Crystal":       (28, 31),
    "Master":        (29, 33),
    "Champion":      (30, 34),
    "Grand Champion": (31, 35),
    "Legend":        (32, 37),
}

MIN_LEVEL_GAP = 2

def dynamic_range(counts: dict[str, int]) -> tuple[int, int]:
    centers, gaps = [], []
    for rank, num in counts.items():
        if num > 0:
            lo, hi = BRACKET_RANGES[rank]
            center = round((lo + hi) / 2)
            gap    = hi - lo
            centers.extend([center] * num)
            gaps.extend([gap] * num)

    # Si no hay jugadores, usa Placement con gap mínimo garantizado
    if not centers:
        lo, hi = BRACKET_RANGES["Placement"]
        if (hi - lo) < MIN_LEVEL_GAP:
            hi = lo + MIN_LEVEL_GAP
        return lo, hi

    avg_center = round(sum(centers) / len(centers))
    # Forzamos un gap mínimo (> 1) sin caer en 1 por redondeos
    avg_gap    = max(MIN_LEVEL_GAP, round(sum(gaps) / len(gaps)))

    low  = max(1, avg_center - avg_gap // 2)
    high = low + avg_gap
    return low, high



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
    "Crystal":  601,
    "Master":   701,
}

class Top100View(View):
    def __init__(self, db_pool: asyncpg.Pool, interaction: discord.Interaction):
        super().__init__(timeout=None)
        self.db       = db_pool
        self.inter    = interaction
        self.page     = 0
        self.per_page = 20

    async def build_embed(self) -> Embed:
        # total hasta 100 y páginas
        total_all = await self.db.fetchval("SELECT COUNT(*) FROM players")
        total     = min(total_all or 0, 100)
        max_page  = (total - 1) // self.per_page if total else 0

        # datos de la página actual
        offset = self.page * self.per_page
        rows = await self.db.fetch(
            "SELECT user_id, mmr, role FROM players ORDER BY mmr DESC OFFSET $1 LIMIT $2",
            offset, self.per_page
        )

        # construye el embed
        embed = Embed(
            title="PJSK LOUGE TOP 100",
            color=0x00B4D8
        )
        header    = "TOP| Name                     |   MMR   | Rank\n"
        separator = "---+--------------------------+---------+-----------\n"
        lines = []
        for idx, r in enumerate(rows, start=offset + 1):
            member = self.inter.guild.get_member(r["user_id"])
            name   = (member.display_name if member else f"ID {r['user_id']}")[:25]
            mmr    = f"{r['mmr']:,}"
            rank   = r["role"]
            lines.append(f"{idx:>2} | {name:<25} | {mmr:>7} | {rank}")

        embed.description = (
            "```\n"
            + header
            + separator
            + "\n".join(lines)
            + f"\n```Página {self.page+1}/{max_page+1}"
        )
        return embed

    @button(emoji="◀️", style=ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        # baja página si puede
        if self.page > 0:
            self.page -= 1

        # recalc habilitación de botones
        total_all = await self.db.fetchval("SELECT COUNT(*) FROM players")
        total     = min(total_all or 0, 100)
        max_page  = (total - 1) // self.per_page if total else 0
        self.prev_page.disabled = (self.page == 0)
        self.next_page.disabled = (self.page == max_page)

        # un único response + edit
        new_embed = await self.build_embed()
        await interaction.response.edit_message(embed=new_embed, view=self)

    @button(emoji="▶️", style=ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        # sube página si puede
        total_all = await self.db.fetchval("SELECT COUNT(*) FROM players")
        total     = min(total_all or 0, 100)
        max_page  = (total - 1) // self.per_page if total else 0
        if self.page < max_page:
            self.page += 1

        # recalc habilitación
        self.prev_page.disabled = (self.page == 0)
        self.next_page.disabled = (self.page == max_page)

        new_embed = await self.build_embed()
        await interaction.response.edit_message(embed=new_embed, view=self)


class Matchmaking(commands.Cog):
    RAW_EN = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-en-diff/main"
    RAW_JP = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-diff/main"
    DIFFS = DIFFICULTY_PRIORITY

    def _find_player_room_id(self, member: discord.Member) -> int | None:
        for rid, info in self.rooms.items():
            if info.get("finished", False):
                continue
            if member in info.get("players", []):
                return rid
        return None

    @staticmethod
    def pairwise_deltas_simple(ratings: list[int], order: list[int], K: int = 30, scale: int = 400, cap: int | None = 39) -> list[int]:
        """
        ratings: MMR actual de cada jugador en el mismo orden que players_list
        order: índices en orden final (0..n-1). Ej: [2,0,1] ⇒ 1º el jugador 2, 2º el 0, 3º el 1
        K fijo. Con K fijo, la suma de Δ tiende a 0 antes del cap.
        """
        n = len(ratings)
        if n <= 1:
            return [0] * n

        Kp = K / (n - 1)  # K por duelo
        # posición final por jugador (0 = 1º lugar)
        pos = [0] * n
        for place, idx in enumerate(order):
            pos[idx] = place

        deltas = [0.0] * n
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                S_i = 1.0 if pos[i] < pos[j] else 0.0
                E_i = 1.0 / (1.0 + 10 ** ((ratings[j] - ratings[i]) / scale))
                deltas[i] += Kp * (S_i - E_i)

        out = [int(round(x)) for x in deltas]
        if cap is not None:
            out = [max(-cap, min(cap, d)) for d in out]
        return out

    async def resolve_member_name(self, guild: discord.Guild, user_id: int) -> str:
        """Nick del servidor > global display name > username."""
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return f"ID:{user_id}"
        return member.nick or getattr(member, "global_name", None) or member.name

    async def _render_room_board_text(self, guild: discord.Guild, rid: int, capacity: int = 5) -> str:
        """Construye el texto 'Room X: 2/5 Players · Average MMR YYY' + lista ordenada por MMR."""
        info = self.rooms.get(rid)
        if not info:
            return ""
        players = info["players"]

        # Construye filas (user_id, mmr) y ordena por MMR desc
        rows = []
        for m in players:
            mmr, _ = await self.fetch_player(m.id)
            rows.append({"user_id": m.id, "mmr": int(mmr)})
        rows.sort(key=lambda r: r["mmr"], reverse=True)

        avg = round(sum(r["mmr"] for r in rows) / len(rows)) if rows else 0
        header = f"Room {rid}: {len(rows)}/{capacity} Players · Average MMR {avg}"

        lines = []
        for r in rows:
            name = await self.resolve_member_name(guild, r["user_id"])
            lines.append(f"• {name} ({r['mmr']})")

        ts = int(datetime.datetime.utcnow().timestamp())
        return header + ("\n" + "\n".join(lines) if lines else "") + f"\n\nLast Update <t:{ts}:R>"

    async def update_room_board(self, guild: discord.Guild, rid: int):
        """Crea/edita el mensaje 'tablero' del room en su thread."""
        info = self.rooms.get(rid)
        if not info:
            return
        thread: discord.Thread = info["thread"]
        content = await self._render_room_board_text(guild, rid)

        msg_id = info.get("board_msg_id")
        if msg_id:
            try:
                msg = await thread.fetch_message(msg_id)
                await msg.edit(content=content)
                return
            except discord.NotFound:
                info.pop("board_msg_id", None)

        msg = await thread.send(content)
        try:
            await msg.pin()
        except Exception:
            pass
        info["board_msg_id"] = msg.id

    async def refresh_rooms_board_for_guild(self, guild: discord.Guild):
        """Re-renderiza todos los rooms del guild (para cambios de nombres)."""
        for rid, info in list(self.rooms.items()):
            if info["thread"].guild.id == guild.id:
                await self.update_room_board(guild, rid)


    @staticmethod
    def _range_for_counts(counts: dict[str, int]) -> tuple[int, int]:
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
                        f"{member.mention} has been kicked due to inactivity"
                    )
                    if players:
                        await self.update_room_board(thread.guild, rid)

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
        # 1) Reset de inactividad en hilos privados
        if isinstance(message.channel, Thread) and not message.author.bot:
            now = datetime.datetime.utcnow()
            data = self.inactivity.setdefault(message.channel.id, {})
            entry = data.setdefault(
                message.author.id,
                {"last": now, "warned_at": None}
            )
            entry["last"] = now
            entry["warned_at"] = None

            # 2) Borrar TODO mensaje con mención
            if message.mentions or message.role_mentions or message.mention_everyone:
                try:
                    await message.delete()
                except:
                    pass
                return

        # 3) Ignorar mensajes del bot
        if message.author.bot:
            return

        # — Aquí iría cualquier otra cosa que quieras ejecutar por mensaje —

        # 4) ¡Procesar comandos!
        await self.bot.process_commands(message)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # cambio de nickname en el servidor
        if before.nick != after.nick:
            await self.refresh_rooms_board_for_guild(after.guild)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        # cambio de display name/username global
        for g in self.bot.guilds:
            if g.get_member(after.id):
                await self.refresh_rooms_board_for_guild(g)


    async def cog_load(self):
        # Pool global de PostgreSQL
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with self.db_pool.acquire() as conn:
            # Jugadores
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    mmr     INTEGER DEFAULT 0,
                    role    TEXT    DEFAULT 'Placement',
                    country TEXT    DEFAULT ''
                );
                """
            )
            # (Compat) por si faltaba la columna country en instalaciones previas
            await conn.execute(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS country TEXT DEFAULT '';"
            )

            # Canciones (catálogo global). Si ya existe, no re-crea.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS songs (
                    id           INTEGER NOT NULL,
                    title        TEXT    NOT NULL,
                    diff         TEXT    NOT NULL,
                    level        INTEGER NOT NULL,
                    notes_count  INTEGER NOT NULL DEFAULT 0
                );
            """)

            # — 1) Elimina duplicados previos (deja 1 fila por (id,diff)) —
            await conn.execute("""
                DELETE FROM songs a
                USING songs b
                WHERE a.id = b.id
                AND a.diff = b.diff
                AND a.ctid > b.ctid;
            """)

            # — 2) Crea restricción única para que ON CONFLICT funcione —
            await conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM   pg_constraint
                        WHERE  conname = 'songs_id_diff_key'
                        AND conrelid = 'songs'::regclass
                    ) THEN
                        ALTER TABLE songs
                        ADD CONSTRAINT songs_id_diff_key UNIQUE (id, diff);
                    END IF;
                END$$;
            """)

            # — 3) Índices útiles —
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_level ON songs(level);")


        # arranca el loop que refresca las canciones
        self.refresh_songs.start()
        await self.refresh_songs()


    async def cog_unload(self):
        await self.db_pool.close()

    @tasks.loop(hours=1)
    async def refresh_songs(self):
        async with self._songs_lock:
            async with aiohttp.ClientSession() as s:
                async def grab(base: str, name: str):
                    url = f"{base}/{name}.json"
                    assert url.startswith("http"), f"Invalid base URL: {base}"
                    async with s.get(url, timeout=20) as r:
                        r.raise_for_status()
                        return await r.json(content_type=None)

                try:
                    # Catálogo GLOBAL (EN)
                    musics = await grab(self.RAW_EN, "musics")
                    diffs  = await grab(self.RAW_EN, "musicDifficulties")
                except Exception as e:
                    log.warning(f"[songs] EN fetch failed ({e}); trying JP fallback…")
                    musics = await grab(self.RAW_JP, "musics")
                    diffs  = await grab(self.RAW_JP, "musicDifficulties")

            title_by_id = {m["id"]: m["title"] for m in musics}

            rows = []
            for d in diffs:
                if d.get("musicDifficulty") not in DIFFICULTY_PRIORITY:
                    continue

                # Total de notas robusto (mantengo tu lógica)
                notes_total = d.get("notesCount", None)
                if isinstance(notes_total, dict):
                    notes_total = sum(v for v in notes_total.values() if isinstance(v, int))
                elif isinstance(notes_total, list):
                    t = 0
                    for it in notes_total:
                        if isinstance(it, dict):
                            v = it.get("count") or it.get("notes") or 0
                            if isinstance(v, int):
                                t += v
                    notes_total = t
                if not isinstance(notes_total, int):
                    notes_total = d.get("noteCount") or d.get("totalNoteCount") or 0
                    notes_map   = d.get("notes")
                    if isinstance(notes_map, dict):
                        notes_total = sum(v for v in notes_map.values() if isinstance(v, int))

                rows.append((
                    d["musicId"],
                    title_by_id.get(d["musicId"], f"ID {d['musicId']}"),
                    d["musicDifficulty"],
                    int(d["playLevel"]),
                    int(notes_total or 0),
                ))

            async with self.db_pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO songs (id, title, diff, level, notes_count)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT ON CONSTRAINT songs_id_diff_key DO UPDATE
                    SET title = EXCLUDED.title,
                        level = EXCLUDED.level,
                        notes_count = EXCLUDED.notes_count
                    """,
                    rows
                )
        print("[songs] Catálogo global actualizado (UPSERT)")


    @refresh_songs.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _get_9_songs(self, low, high, diffs=None):
        if diffs is None:
            diffs = DIFFICULTY_PRIORITY

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                placeholder = ",".join(f"${i+3}" for i in range(len(diffs)))
                rows = await conn.fetch(
                    f"""SELECT title, level, diff, notes_count
                        FROM songs
                        WHERE level BETWEEN $1 AND $2
                        AND diff IN ({placeholder})""",
                    low, high, *diffs
                )

        # Agrupar por nivel
        songs_by_lvl = {}
        for r in rows:
            lvl = r["level"]
            songs_by_lvl.setdefault(lvl, []).append(
                (r["title"], lvl, r["diff"].capitalize(), int(r["notes_count"]))
            )

        picks = []
        # Recorre niveles de mayor a menor y toma hasta 3 al azar por nivel
        for lvl in range(high, low - 1, -1):
            pool = songs_by_lvl.get(lvl, [])
            if not pool:
                continue
            k = min(3, len(pool))
            picks.extend(random.sample(pool, k))

        # Mezcla global y recorta a 9
        random.shuffle(picks)
        return picks[:9]




    async def launch_song_poll(self, room_info):
        thread  = room_info["thread"]
        players = room_info["players"]

        # — 1) Cuenta cuántos jugadores hay de cada rango —
        counts = { rank: 0 for rank in BRACKET_RANGES }
        for m in players:
            user_rank = next(
                (r for r in BRACKET_RANGES if discord.utils.get(m.roles, name=r)),
                "Placement"
            )
            counts[user_rank] += 1

        # 2) Elige low/high según media de centros
        low, high = dynamic_range(counts)

        # 2.1) Determinar diffs permitidos según el rango resultante
        allowed_diffs = DIFFICULTY_PRIORITY
        for rank, (lo, hi) in BRACKET_RANGES.items():
            if lo == low and hi == high:
                allowed_diffs = ALLOWED_DIFFS_BY_RANK[rank]
                break

        # 3) Obtén el pool de canciones
        all_songs = await self._get_9_songs(low, high, allowed_diffs)

        # 3.1) Suma total de notas para validación posterior
        picks = all_songs[:5]
        random.shuffle(picks)
        total_notes = sum(song[3] for song in picks)  # índice 3 = notes_count
        room_info["total_notes"] = total_notes

        # 4) Construye el mensaje con notas individuales y total
        song_lines = "\n".join(
            f"{i+1}. **{title}** (Lv {lvl} {diff}) [{notes} notas]"
            for i, (title, lvl, diff, notes) in enumerate(picks)
        )


        await thread.send(
            f"Selected songs (Lv {low}–{high}) "
            f"(Total {total_notes} notes) 🎶\n{song_lines}"
        )

        room_info["started"] = True
        room_info["closed"]  = True        # ← NUEVO: cerrar para nuevos /c
        self.inactivity.pop(thread.id, None)  # ← NUEVO: deja de pingear AFK de inmediato


    @app_commands.command(
        name="start",
        description="Inicia la votación de 5 canciones"
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
                "🔸 Room must have between 2-5 players",
                ephemeral=True
            )

        # — 4) Cuenta cuántos jugadores hay de cada rango —
        counts = { rank: 0 for rank in BRACKET_RANGES }
        for m in players:
            user_rank = next(
                (r for r in BRACKET_RANGES if discord.utils.get(m.roles, name=r)),
                "Placement"
            )
            counts[user_rank] += 1

        # — 5) Elige low/high según media de centros —
        lo, hi = dynamic_range(counts)

        # — 8) Envía la vista en el hilo correspondiente —
        if is_join_thread:
            thread = ch
        else:
            join_chan = ch
            players   = [m for m in join_chan.members if not m.bot]
            thread = await join_chan.create_thread(
                name=f"Sala {len(self.rooms)+1} ({lo}–{hi}★)",
                auto_archive_duration=60,
                type=discord.ChannelType.public_thread
            )
            new_id = max(self.rooms.keys(), default=0) + 1
            self.rooms[new_id] = {
                "players": players.copy(),
                "thread": thread,
                "category_id": join_chan.category_id or 0,
                "closed": True,               # ← ya cerrada para que /c no meta más
            }
            room_entry = self.rooms[new_id]   # ← IMPORTANTE



        # — 6) Filtrar diffs según el rango (lo, hi)
        allowed_diffs = DIFFICULTY_PRIORITY
        for rank,(rlo,rhi) in BRACKET_RANGES.items():
            if rlo == lo and rhi == hi:
                allowed_diffs = ALLOWED_DIFFS_BY_RANK[rank]
                break

        # — 6.1) Obtener 5 canciones y mezclar
        all_songs = await self._get_9_songs(lo, hi, allowed_diffs)
        picks     = all_songs[:5]
        random.shuffle(picks)

        # — 6.2) Sumar total de notas y guardar en la sala
        total_notes               = sum(song[3] for song in picks)
        room_entry["total_notes"] = total_notes

        # — 7) Enviar UN SOLO mensaje con rango, diffs y total de notas
        # … dentro de start()
        song_lines = "\n".join(
            f"{i+1}. **{t}** (Lv {lvl} {df}) [{nts} notas]"
            for i,(t,lvl,df,nts) in enumerate(picks)
        )


        await thread.send(
            f"Selected songs (Lv {lo}–{hi}) "
            f"(Total {total_notes} notes) 🎶\n{song_lines}"
        )

        room_entry["started"] = True       # ← NUEVO
        room_entry["closed"]  = True       # ← NUEVO
        self.inactivity.pop(thread.id, None)  # ← NUEVO



        # — 9) Confirmación efímera al invocador —
        await interaction.response.send_message(
            f"✅ Votación iniciada en {thread.mention}", ephemeral=True
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

                # Re-render para que el encabezado del tablero refleje el nuevo índice
        for rid in self.rooms.keys():
            try:
                await self.update_room_board(guild, rid)
            except Exception:
                pass


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

        member = interaction.user

        # ⛔️ Guard: ya estás en una sala activa
        already = self._find_player_room_id(member)
        if already is not None:
            return await interaction.response.send_message(
                f"❌ You are already in room {already}. Use **/d** to leave before joining another.",
                ephemeral=True
            )

        # Determinar la categoría padre (si viene de Thread)
        parent_chan = ch
        if isinstance(parent_chan, Thread) and parent_chan.parent:
            parent_chan = parent_chan.parent
        current_cat = parent_chan.category_id or 0

        mmr_val, _ = await self.fetch_player(member.id)

        # Buscar sala NO cerrada en esta categoría
        best_rid = None
        for rid, info in self.rooms.items():
            if (
                info["category_id"] == current_cat
                and not info.get("closed", False)      # ignorar salas cerradas
                and not info.get("started", False)     # no meter a salas iniciadas
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
                "players": [],
                "thread": thread,
                "category_id": current_cat,
                "closed": False,
                "started": False,
                "finished": False,
            }
            best_rid = new_id

            # Borrar el aviso automático del sistema
            async for msg in ch.history(limit=5):
                if msg.type == MessageType.thread_created and msg.author == interaction.user:
                    await msg.delete()
                    break

            self.bot.dispatch('room_updated', best_rid)

        # Añadir jugador
        room = self.rooms[best_rid]
        if member in room["players"]:
            return await interaction.response.send_message(
                "You are already in this room", ephemeral=True
            )
        room["players"].append(member)

        await interaction.response.send_message(
            f"Joined room {best_rid}.", ephemeral=True
        )
        await ch.send(f"**{member.display_name}** Joined room {best_rid} (MMR {mmr_val})")
        await room["thread"].add_user(member)
        await self.update_room_board(interaction.guild, best_rid)

        await self.sort_and_rename_rooms(interaction.guild)

        # Auto-start si llega a 5
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
                await info["thread"].send(f"**{member.display_name}** left")
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
                else:
                    await self.update_room_board(interaction.guild, rid)


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
                f"{name} has {mmr_val} MMR and rank {role}."
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
    
    @app_commands.command(name="top100", description="Top 100 jugadores por MMR con paginación")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def top100(self, interaction: discord.Interaction):
        view = Top100View(self.db_pool, interaction)
        total    = await self.db_pool.fetchval("SELECT COUNT(*) FROM players")
        max_page = (total - 1) // view.per_page if total else 0
        view.prev_page.disabled = True
        view.next_page.disabled = (0 == max_page)

        embed = await view.build_embed()
        await interaction.response.send_message(embed=embed, view=view)


    @commands.command(name="debug_diffs")
    async def debug_diffs(self, ctx: commands.Context):
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT diff FROM songs")
        diffs = [r["diff"] for r in rows]
        await ctx.send(f"Dificultades en la tabla songs: {diffs}")

    @commands.command(name="debug_poll")
    async def debug_poll(self, ctx: commands.Context):
        songs = await self._get_9_songs(1, 99)
        await ctx.send(f"🎵 debug_poll sacó {len(songs)} canciones")
        if not songs:
            return await ctx.send("⚠️ No hay canciones en debug_poll.")
        random.shuffle(songs)
        await ctx.send(
            "🎵 Canciones aleatorias:\n" + "\n".join(
                f"{i+1}. **{title}** (Lv {level} {diff})"
                for i, (title, level, diff) in enumerate(songs[:5])
            )
        )

    @staticmethod
    def get_role_from_notes(stats):
        # stats: [perfect, great, good, bad, miss]
        perfect, great, good, bad, miss = stats
        if perfect > 0 and great == 0 and good == 0 and bad == 0 and miss == 0:
            return "Master"
        total = great + good + bad + miss
        if total <= 5: return "Crystal"
        elif total <= 15: return "Diamond"
        elif total <= 30: return "Platinum"
        elif total <= 50: return "Gold"
        elif total <= 100: return "Silver"
        elif total <= 250: return "Bronze"
        else: return "Iron"


    @commands.command(name="submit")
    async def submit(self, ctx: commands.Context, *, block: str):
        mm = self.bot.get_cog("Matchmaking")
        room = next((r for r in mm.rooms.values() if r["thread"].id == ctx.channel.id), None)
        if not room:
            return await ctx.send("Only works inside a room thread")

        # Valida que la suma de stats por línea coincida con el total de notas esperado
        expected = room.get("total_notes")
        if expected is not None:
            lines_raw = [l.strip() for l in block.split("\n") if l.strip()]
            for idx, ln in enumerate(lines_raw, start=1):
                stats = list(map(int, ln.rsplit(" ", 1)[-1].split(",")))
                if sum(stats) != expected:
                    return await ctx.send(
                        f"there is an error on the calculation on line {idx}: "
                        f"{sum(stats)} ≠ {expected}"
                    )

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

        # Solo pueden votar los jugadores de la sala
        allowed_voters = {m.id for m in players}
        threshold = len(allowed_voters) // 2 + 1

        counts = {"✅": 0, "❎": 0}
        voted  = set()

        def check(reaction, user):
            return (
                reaction.message.id == vote_msg.id
                and not user.bot
                and user.id in allowed_voters
                and str(reaction.emoji) in ("✅", "❎")
            )

        ganador = None
        deadline = asyncio.get_event_loop().time() + 60.0  # ventana de 60s

        while ganador is None:
            timeout = max(0.0, deadline - asyncio.get_event_loop().time())
            if timeout == 0.0:
                break
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=timeout, check=check)
            except asyncio.TimeoutError:
                break

            # evita votos múltiples del mismo usuario
            if user.id in voted:
                continue

            choice = str(reaction.emoji)
            counts[choice] += 1
            voted.add(user.id)

            if counts["✅"] >= threshold:
                ganador = "✅"
            elif counts["❎"] >= threshold:
                ganador = "❎"

        # Si no alcanzó el umbral, decide por mayoría simple; empate/0 votos = rechazo
        if ganador is None:
            if counts["✅"] > counts["❎"]:
                ganador = "✅"
            elif counts["❎"] > counts["✅"]:
                ganador = "❎"
            else:
                ganador = "❎"

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
            # ctx.send(f"PLAYER: {current_role}")
        # ——————————————————————————————
        # FASE 1: bonus fijo para Placement
        # ——————————————————————————————
        for p in players_list:
            if p["role"] == "Placement":
                bonus_role      = self.get_role_from_notes(p["stats"])
                p["mmr_actual"] = PLACEMENT_MMR_BONUS[bonus_role]
            else:
                p["mmr_actual"] = p["old"]

        # ——————————————————————————————
        # ——————————————————————————————
        # FASE 2 (NUEVA): Elo por pares con K fijo (sin 'games')
        # ——————————————————————————————
        # players_list YA está ORDENADA por puntos (1º…n)
        # Ordenar por puntos (total), mayor a menor
        players_list.sort(key=lambda x: x["total"], reverse=True)
        ratings = [p["mmr_actual"] for p in players_list]
        order   = list(range(len(players_list)))  # 0..n-1 ya en orden final
        deltas  = self.pairwise_deltas_simple(ratings, order, K=30, scale=400, cap=39)
        await ctx.send(f"PLAYER LIST: {players_list}")
        await ctx.send(f"PLAYER LIST DELTA:{deltas}")
        summary = []
        for (idx, p), mmr_delta in zip(enumerate(players_list, start=1), deltas):
            mmr_prev  = p["mmr_actual"]
            mmr_final = mmr_prev + mmr_delta
            role_name = get_rank_from_mmr(mmr_final)
            
            await ctx.send(f"MEMBER ID: {p["member"].id}")
            await ctx.send(f"MMR FINAL: {mmr_final}")
            await ctx.send(f"MMR DELTA: {mmr_delta}")
            # Actualiza BD (sin 'games')
            await self.db_pool.execute(
                "UPDATE players SET mmr=$1, role=$2, country=$3 WHERE user_id=$4",
                mmr_final, role_name, p["cc"], p["member"].id
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))
