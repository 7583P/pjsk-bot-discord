import os
import re
import random
import asyncio
import datetime
import aiohttp                 # descarga catálogo canciones
import asyncpg                 # PostgreSQL asíncrono
import discord
import logging
from discord import app_commands, Embed
from discord import ui, ButtonStyle
from discord import Thread, TextChannel, MessageType
from discord.ext import commands, tasks   # loops periódicos
from discord.ui import View, button
log = logging.getLogger(__name__)
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

ISO2_SUFFIX_RE = re.compile(r"\b([A-Za-z]{2})$")

def flagify_iso2_suffix(text: str) -> str:
    """
    Si el nombre termina con dos letras ISO2 (ej. 'Reimu Hakurei pe'),
    reemplaza esas letras por la bandera (🇵🇪).
    """
    t = text.strip()
    m = ISO2_SUFFIX_RE.search(t)
    if not m:
        return t
    return t[:m.start(1)] + country_flag(m.group(1).upper())


# Prioridad global de dificultades, ahora incluyendo hard
DIFFICULTY_PRIORITY = ("hard", "expert", "master", "append")

# Mapeo de rangos a dificultades permitidas
ALLOWED_DIFFS_BY_RANK = {
    "Placement":     ("expert",),
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
    "Placement":     (24, 28),
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

MIN_LEVEL_GAP = 3

VOTE_TIMEOUT_SUBMIT = 30 

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

    if mmr <= 99:
        return "Iron"
    elif mmr <= 199:
        return "Bronze"
    elif mmr <= 299:
        return "Silver"
    elif mmr <= 399:
        return "Gold"
    elif mmr <= 499:
        return "Platinum"
    elif mmr <= 599:
        return "Diamond"
    elif mmr <= 699:
        return "Crystal"
    elif mmr <= 799:
        return "Master"
    elif mmr <= 899:
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

class Top100View(View):
    def __init__(self, db_pool: asyncpg.Pool, interaction: discord.Interaction, season_id: int):
        super().__init__(timeout=None)
        self.db       = db_pool
        self.inter    = interaction
        self.season_id = season_id
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
            "SELECT p.user_id, sp.mmr, sp.role FROM players p inner join seasonplayers sp on p.user_id = sp.user_id where sp.season_id = $3 ORDER BY sp.mmr DESC OFFSET $1 LIMIT $2",
            offset, self.per_page, self.season_id
        )

        # construye el embed
        embed = Embed(
            title="PJSK LOUNGE TOP 100",
            color=0x00fffb
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
    
    async def _get_random_songs(self, low: int, high: int, diffs: tuple[str, ...], limit: int, exclude_titles: set[str] | None = None):
        """
        Devuelve hasta 'limit' canciones aleatorias (title, level, diff_capitalized, notes) en [low, high] y diffs dadas.
        """
        exclude_titles = exclude_titles or set()
        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                placeholders = ",".join(f"${i+3}" for i in range(len(diffs)))
                params = [low, high, *diffs]
                extra_where = ""
                if exclude_titles:
                    notin = ",".join(f"${len(params)+i+1}" for i in range(len(exclude_titles)))
                    extra_where = f" AND title NOT IN ({notin})"
                    params.extend(list(exclude_titles))

                rows = await conn.fetch(
                    f"""
                    SELECT title, level, diff, notes_count
                    FROM songs
                    WHERE level BETWEEN $1 AND $2
                    AND diff IN ({placeholders})
                    {extra_where}
                    ORDER BY random()
                    LIMIT {int(limit)}
                    """,
                    *params
                )

        out = []
        for r in rows:
            out.append((r["title"], int(r["level"]), r["diff"].capitalize(), int(r["notes_count"])))
        return out

    async def _pick_placement_songs_levels_24_to_28(self) -> list[tuple[str, int, str, int]]:
        """
        Regla especial Placement:
        - EXACTAMENTE 1 canción por nivel: 24, 25, 26, 27 y 28
        - Solo EXPERT
        - Evita repetir títulos
        - Si un nivel no tiene canciones Expert en el catálogo, intenta fallback aleatorio dentro 24–28.
        """
        levels = [24, 25, 26, 27, 28]
        picks: list[tuple[str, int, str, int]] = []
        chosen_titles: set[str] = set()

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                for L in levels:
                    # Intenta 1 canción EXACTA de ese nivel (Expert/Master)
                    rows = await conn.fetch(
                        """
                        SELECT title, level, diff, notes_count
                        FROM songs
                        WHERE level = $1
                          AND diff IN ('expert')
                        ORDER BY random()
                        LIMIT 5
                        """,
                        L
                    )

                    # toma la primera cuyo título no esté usado
                    picked = None
                    for r in rows:
                        if r["title"] not in chosen_titles:
                            picked = r
                            break

                    if picked is None:
                        # Fallback: cualquiera en 24–28 Expert/Master que no repita título
                        fb = await conn.fetch(
                            """
                            SELECT title, level, diff, notes_count
                            FROM songs
                            WHERE level BETWEEN 24 AND 28
                              AND diff IN ('expert')
                            ORDER BY random()
                            LIMIT 10
                            """
                        )
                        for r in fb:
                            if r["title"] not in chosen_titles:
                                picked = r
                                break

                    if picked:
                        chosen_titles.add(picked["title"])
                        picks.append((
                            picked["title"],
                            int(picked["level"]),
                            picked["diff"].capitalize(),
                            int(picked["notes_count"])
                        ))
                    # Si ni siquiera el fallback trae algo, simplemente sigue (dejará <5, muy raro)

        # Mezcla para no salir siempre en orden 24→28
        random.shuffle(picks)
        return picks[:5]




    
    def _format_results_board(self, summary_rows: list[tuple]) -> str:
        """
        summary_rows = [
        (pos_str, name_with_flag, total_points, stats_list, mmr_prev, mmr_delta, mmr_final),
        ...
        ]

        Dibuja la tabla:
        POS | PLAYER | PGGBM | POINTS | MMR (+/-)
        """
        # Anchos de columnas (ajústalos si tuvieras nombres larguísimos)
        W_POS    = 4
        W_PLAYER = 24
        W_PGGBM  = 13   # "(5553,1,0,0,0)"
        W_POINTS = 7

        # Encabezado y separador (igual estilo que top100)
        colh = (
            f"{'POS':<{W_POS}}| "
            f"{'PLAYER':<{W_PLAYER}}| "
            f"{'PGGBM':<{W_PGGBM}}| "
            f"{'POINTS':>{W_POINTS}}| "
            f"MMR (+/-)"
        )
        sep = (
            f"{'-'*W_POS}+"
            f"{'-'*(W_PLAYER+1)}+"
            f"{'-'*(W_PGGBM+1)}+"
            f"{'-'*(W_POINTS+1)}+"
            f"{'-'*9}"
        )

        lines = []
        for pos, name_with_flag, pts, stats, mmr_prev, mmr_delta, mmr_final in summary_rows:
            # Convierte 'pe', 'jp', 'us' finales → 🇵🇪 🇯🇵 🇺🇸
            safe_name = flagify_iso2_suffix(name_with_flag)

            pggbm = f"({stats[0]},{stats[1]},{stats[2]},{stats[3]},{stats[4]})"
            lines.append(
                f"{str(pos):<{W_POS}}| "
                f"{safe_name:<{W_PLAYER}}| "
                f"{pggbm:<{W_PGGBM}}| "
                f"{pts:>{W_POINTS}}| "
                f"{mmr_prev:>4} {mmr_delta:+3d} = {mmr_final:<4}"
            )


        body = "\n".join(lines) if lines else "(sin filas)"
        # Solo retorna el bloque monoespaciado (sin título). El título irá en el Embed.
        return "```\n" + colh + "\n" + sep + "\n" + body + "\n```"

    async def _resolve_season_id(self, season_choice: str | None) -> tuple[int | None, str]:
        """
        Devuelve (season_id, season_name) según la elección del usuario.
        - None o "actual"  => temporada activa (get_current_season)
        - "beta"           => busca por nombre 'beta'
        """
        if (season_choice or "actual").lower() in ("actual", "current"):
            row = await self.get_current_season()
            return (row["id"], row["name"]) if row else (None, "N/A")

        # Beta por nombre (case-insensitive)
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name FROM seasons WHERE LOWER(name) = 'beta' ORDER BY startdate LIMIT 1"
            )
        return (row["id"], row["name"]) if row else (None, "Beta")

    async def _fetch_user_stats(self, user_id: int, season_id: int):
        """
        Devuelve (pos, total, mmr, role) para user_id en la season dada, o None si no existe.
        """
        sql = """
        WITH ranked AS (
          SELECT
            sp.user_id,
            sp.mmr,
            sp.role,
            RANK() OVER (ORDER BY sp.mmr DESC) AS pos
          FROM seasonplayers sp
          WHERE sp.season_id = $2
        )
        SELECT r.pos, (SELECT COUNT(*) FROM seasonplayers WHERE season_id = $2) AS total,
               r.mmr, r.role
        FROM ranked r
        WHERE r.user_id = $1
        """
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(sql, user_id, season_id)
        if not row:
            return None
        return int(row["pos"]), int(row["total"]), int(row["mmr"]), row["role"]

    async def _fetch_cutoff(self, season_id: int, rank_n: int) -> int:
        """
        Devuelve el MMR del puesto N (rank_n) en la season dada.
        Si no existe ese puesto, devuelve 0.
        """
        sql = """
            SELECT sp.mmr
            FROM seasonplayers sp
            WHERE sp.season_id = $1
            ORDER BY sp.mmr DESC
            OFFSET $2 LIMIT 1
        """
        async with self.db_pool.acquire() as conn:
            val = await conn.fetchval(sql, season_id, max(0, rank_n - 1))
        return int(val) if val is not None else 0

    def _format_cutoffs_board(self, season_name: str, data: dict[int, int]) -> str:
        """
        data: {10:mmr, 20:mmr, 30:mmr, 40:mmr, 50:mmr, 100:mmr}
        Devuelve el board monoespaciado.
        """
        header = f"CUTOFFS — Season {season_name}"
        colh   = "Tier |   MMR"
        sep    = "-----+-------"
        lines  = []
        for n in (10, 20, 30, 40, 50, 100):
            mmr = data.get(n, 0)
            lines.append(f"T{n:<3}| {mmr:>6}")
        body = "\n".join(lines) if lines else "(sin datos)"
        return "```\n" + header + "\n" + colh + "\n" + sep + "\n" + body + "\n```"



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
        """Construye el texto 'Room X: N/5 Players · Average MMR YYY · [STATUS] (hora si aplica)' + lista por MMR."""
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

        started = info.get("started", False)
        started_at = info.get("started_at")   # epoch int si existe
        if info.get("finished", False):
            status = "[FINISHED]"
        elif started:
            ts_part = f" <t:{int(started_at)}:T>" if started_at else ""
            status = f"[STARTED]{ts_part}"
        else:
            status = "[NOT STARTED]"


        header = f"Room {rid}: {len(rows)}/{capacity} Players · Average MMR {avg} · {status}"

        lines = []
        for r in rows:
            name = await self.resolve_member_name(guild, r["user_id"])
            lines.append(f"• {name} ({r['mmr']})")

        ts = int(datetime.datetime.utcnow().timestamp())
        return header + ("\n" + "\n".join(lines) if lines else "") + f"\n\nLast Update <t:{ts}:R>"
    
    async def close_room(self, thread: discord.Thread, *, delay: int = 60, delete: bool = True):
        """Cierra/borra la sala y la saca de self.rooms; avisa a Rooms."""
        if not isinstance(thread, discord.Thread):
            return
        if getattr(self, "_closing_threads", None) is None:
            self._closing_threads = set()
        if thread.id in self._closing_threads:
            return

        self._closing_threads.add(thread.id)
        try:
            await asyncio.sleep(delay)
            # 1) Intenta cerrar/borrar el hilo
            if delete:
                await thread.delete(reason="Room auto-deleted after results")
            else:
                await thread.edit(archived=True, locked=True)

        except discord.Forbidden:
            log.error("Faltan permisos: Manage Threads para eliminar/editar hilos.")
        except Exception:
            log.exception("close_room falló para %s", thread.id)
        finally:
            # 2) Saquemos la sala del dict y notifiquemos a Rooms
            rid = next((rid for rid, info in self.rooms.items() if info["thread"].id == thread.id), None)
            if rid is not None:
                self.rooms.pop(rid, None)
                try:
                    self.bot.dispatch('room_finished', rid)
                except Exception:
                    pass
            self._closing_threads.discard(thread.id)




    async def update_room_board(self, guild: discord.Guild, rid: int):
        info = self.rooms.get(rid)
        if not info:
            return
        # No intentes refrescar tableros de salas terminadas
        if info.get("finished", False):
            return

        thread: discord.Thread = info["thread"]
        try:
            content = await self._render_room_board_text(guild, rid)
            msg_id = info.get("board_msg_id")

            if msg_id:
                msg = await thread.fetch_message(msg_id)
                await msg.edit(content=content)
                return

            msg = await thread.send(content)
            try:
                await msg.pin()
            except Exception:
                pass
            info["board_msg_id"] = msg.id

        except (discord.NotFound, discord.Forbidden):
            # El hilo ya no existe o no tenemos permisos: quita la sala y avisa
            dead_rid = next((k for k, v in self.rooms.items() if v is info), None)
            if dead_rid is not None:
                self.rooms.pop(dead_rid, None)
                self.bot.dispatch('room_finished', dead_rid)
        except Exception:
            log.exception("update_room_board falló para rid=%s", rid)


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
                        f"{member.mention} 5 minutes have passed, type something within 2 minutes to stay in the room"
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
        # Ignorar bots
        if message.author.bot:
            return

        # Si es un thread: reset de inactividad + control de menciones
        if isinstance(message.channel, Thread):
            now = datetime.datetime.utcnow()
            data = self.inactivity.setdefault(message.channel.id, {})
            entry = data.setdefault(message.author.id, {"last": now, "warned_at": None})
            entry["last"] = now
            entry["warned_at"] = None

            # ——— BLOQUEO DE PINGS EN HILOS DE SALA ———
            room = next((r for r in self.rooms.values() if r["thread"].id == message.channel.id), None)
            if room:
                allowed_ids = {m.id for m in room["players"]}

                # Bloquear @everyone / @here y pings a roles SIEMPRE
                if message.mention_everyone or message.role_mentions:
                    try:
                        await message.delete()
                    except:
                        pass
                    return

                # Permitir @usuarios solo si TODOS están en la sala
                if message.mentions:
                    if any(u.id not in allowed_ids for u in message.mentions):
                        try:
                            await message.delete()
                        except:
                            pass
                        return
                    

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
        # Evitar re-selección si ya fue iniciada
        if room_info.get("started"):
            try:
                await room_info["thread"].send("songs has been selected")
            except Exception:
                pass
            return

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

        # — 2) FORZAR GAP DE PLACEMENT SI HAY ALGUNO EN LA SALA —
        if counts.get("Placement", 0) > 0:
            low, high = BRACKET_RANGES["Placement"]  # (25, 28)
        else:
            low, high = dynamic_range(counts)

        # — 2.1) Dificultades permitidas según el (low,high) elegido —
        #        (si se forzó Placement, esto devolverá ("hard","expert","master"))
        allowed_diffs = DIFFICULTY_PRIORITY
        for rank, (lo_r, hi_r) in BRACKET_RANGES.items():
            if lo_r == low and hi_r == high:
                allowed_diffs = ALLOWED_DIFFS_BY_RANK[rank]
                break

        # 🚫 Regla adicional: si hay al menos un Placement en la sala, quitar "append"
        if counts.get("Placement", 0) > 0:
            allowed_diffs = tuple(d for d in allowed_diffs if d != "append")


        # 3) Selección de canciones
        if counts.get("Placement", 0) > 0:
            # 1 canción por nivel 24–28, solo Expert/Master
            picks = await self._pick_placement_songs_levels_24_to_28()
            # Mostrar explícitamente el rango especial
            low, high = 24, 28
        else:
            all_songs = await self._get_9_songs(low, high, allowed_diffs)
            picks = all_songs[:5]
            random.shuffle(picks)


        # 4) Construir el mensaje
        total_notes = sum(int(n or 0) for *_, n in picks)  # suma segura

        song_lines = "\n".join(
            f"{i}. **{title}** (Lv {lvl} {diff}) [{notes} notas]"
            for i, (title, lvl, diff, notes) in enumerate(picks, start=1)
        )

        await thread.send(
            f"Selected songs (Lv {low}–{high})\n"
            f"(Total {total_notes} notes)\n"
            f"{song_lines}"
        )

        # ——— MARCAR INICIO + TIMESTAMP ———
        room_info["started"] = True
        room_info["closed"]  = True
        self.inactivity.pop(thread.id, None)
        room_info.setdefault("started_at", int(datetime.datetime.utcnow().timestamp()))
        room_info.setdefault("started_ts", room_info["started_at"])  # ← para #rooms

        # refrescar el tablero del room y #rooms
        rid = next((rid for rid, r in self.rooms.items() if r["thread"].id == thread.id), None)
        if rid is not None:
            await self.update_room_board(thread.guild, rid)
            self.bot.dispatch('room_updated', rid)  # ← para actualizar #rooms al instante



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

            # ⛔ Ya se inició en esta sala
            if room_entry.get("started"):
                return await interaction.response.send_message("songs has been selected", ephemeral=True)

            players = room_entry["players"]

        else:
            join_chan = ch
            players   = [m for m in join_chan.members if not m.bot]

        # — 3) Validar 2–5 jugadores —
        if not (3 <= len(players) <= 5):
            return await interaction.response.send_message(
                "Room must have between 3-5 players",
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

        # — 5) FORZAR GAP DE PLACEMENT SI HAY ALGUNO EN LA SALA —
        if counts.get("Placement", 0) > 0:
            lo, hi = BRACKET_RANGES["Placement"]  # (25, 28)
        else:
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

        # — 6) Filtrar diffs según el rango (lo, hi) —
        allowed_diffs = DIFFICULTY_PRIORITY
        for rank, (rlo, rhi) in BRACKET_RANGES.items():
            if rlo == lo and rhi == hi:
                allowed_diffs = ALLOWED_DIFFS_BY_RANK[rank]
                break


        # 🚫 Regla adicional: si hay Placement en la sala, quitar "append"
        if counts.get("Placement", 0) > 0:
            allowed_diffs = tuple(d for d in allowed_diffs if d != "append")

        # — 6.1) Obtener canciones según reglas
        if counts.get("Placement", 0) > 0:
            picks = await self._pick_placement_songs_levels_24_to_28()
            lo, hi = 24, 28  # Mostrar explícitamente el rango especial
        else:
            all_songs = await self._get_9_songs(lo, hi, allowed_diffs)
            picks = all_songs[:5]
            random.shuffle(picks)



        # — 6.2) Sumar total de notas y guardar en la sala —
        total_notes               = sum(song[3] for song in picks)
        room_entry["total_notes"] = total_notes

        # — 7) Enviar UN SOLO mensaje con rango, diffs y total de notas —
        song_lines = "\n".join(
            f"{i+1}. **{t}** (Lv {lvl} {df}) [{nts} notas]"
            for i,(t,lvl,df,nts) in enumerate(picks)
        )
        await thread.send(
            f"Selected songs (Lv {lo}–{hi}) "
            f"(Total {total_notes} notes) 🎶\n{song_lines}"
        )

        # ——— MARCAR INICIO + TIMESTAMP ———
        room_entry["started"] = True
        room_entry["closed"]  = True
        self.inactivity.pop(thread.id, None)
        room_entry.setdefault("started_at", int(datetime.datetime.utcnow().timestamp()))
        room_entry.setdefault("started_ts", room_entry["started_at"])  # ← para #rooms

        # refrescar tablero y #rooms
        rid = next((rid for rid, r in self.rooms.items() if r["thread"].id == thread.id), None)
        if rid is not None:
            await self.update_room_board(interaction.guild, rid)
            self.bot.dispatch('room_updated', rid)  # ← para actualizar #rooms al instante

        # — 9) Confirmación efímera al invocador —
        await interaction.response.send_message(
            f"✅ Votación iniciada en {thread.mention}", ephemeral=True
        )

    async def get_current_season(self) -> asyncpg.Record | None:
        """
        Returns the active season row or None.
        """
        assert self.db_pool is not None, "DB pool not initialized"
        sql = """
            SELECT id, name, active, startdate, enddate
            FROM seasons
            WHERE active = true
            ORDER BY startdate DESC
            LIMIT 1
        """
        async with self.db_pool.acquire() as conn:
            return await conn.fetchrow(sql)

    async def fetch_player(self, user_id: int):
        season = await self.get_current_season()
        season_id = season["id"]

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT sp.mmr, sp.role FROM players p inner join seasonplayers sp on p.user_id = sp.user_id WHERE p.user_id = $1 and sp.season_id  = $2", user_id, season_id
            )
            if row:
                return row["mmr"], row["role"]
            # await conn.execute(
            #     "INSERT INTO players (user_id, mmr, role) VALUES ($1, 0, 'Placement')",
            #     user_id,
            # )
            row = await conn.fetchrow(
                "SELECT sp.mmr, sp.role FROM players p inner join seasonplayers sp on p.user_id = sp.user_id WHERE p.user_id = $1 and sp.season_id  = $2", user_id, season_id
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


    @app_commands.command(name="stats", description="Shows your position, mmr and rank in the selected season")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(season="Season: Current or Beta")
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Beta",   value="beta"),
        ]
    )
    async def stats(self, interaction: discord.Interaction, season: app_commands.Choice[str] | None = None):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message("⚠️ Season not founf", ephemeral=True)

        user_id = interaction.user.id
        data = await self._fetch_user_stats(user_id, season_id)
        if not data:
            return await interaction.response.send_message(
                f"You dont have register in **Season {season_name}**.", ephemeral=True
            )

        pos, total, mmr, role = data
        msg = (
            f"**Stats — Season {season_name}**\n"
            f"• **Player:** {interaction.user.mention}\n"
            f"• **Position:** #{pos} of {total}\n"
            f"• **MMR:** {mmr}\n"
            f"• **Rank:** {role}"
        )
        await interaction.response.send_message(msg)

    @app_commands.command(name="stats_user", description="Shows your position, mmr and rank in the selected season")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(user="Player", season="Season: Current or Beta")
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Beta",   value="beta"),
        ]
    )
    async def stats_user(self, interaction: discord.Interaction, user: discord.Member, season: app_commands.Choice[str] | None = None):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message("⚠️ No encontré la temporada seleccionada.", ephemeral=True)

        data = await self._fetch_user_stats(user.id, season_id)
        if not data:
            return await interaction.response.send_message(
                f"{user.mention} You dont have register in **Season {season_name}**.", ephemeral=True
            )

        pos, total, mmr, role = data
        msg = (
            f"**Stats — Season {season_name}**\n"
            f"• **Player:** {user.mention}\n"
            f"• **Position:** #{pos} of {total}\n"
            f"• **MMR:** {mmr}\n"
            f"• **Rank:** {role}"
        )
        await interaction.response.send_message(msg)

    @app_commands.command(
        name="cutoffs",
        description="Shows the MMR Cutoff of T10, T20, T30, T40, T50 y T100 for the selected season"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(season="Temporada: Current or Beta")
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="Current"),
            app_commands.Choice(name="Beta",   value="beta"),
        ]
    )
    async def cutoffs(self, interaction: discord.Interaction, season: app_commands.Choice[str] | None = None):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message("⚠️ No encontré la temporada seleccionada.", ephemeral=True)

        # calcular cutoffs
        tiers = (10, 20, 30, 40, 50, 100)
        data  = {}
        for n in tiers:
            data[n] = await self._fetch_cutoff(season_id, n)

        board = self._format_cutoffs_board(season_name, data)
        await interaction.response.send_message(board)



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
            # Solo salas cuyo thread pertenezca a un canal #join permitido
            if info["thread"].parent_id not in ALLOWED_JOIN_CHANNELS:
                continue

            if member in info["players"]:
                # ⛔ No permitir salir si la sala ya inició
                if info.get("started"):
                    return await interaction.response.send_message(
                        "You can't leave after the songs have been selected",
                        ephemeral=True
                    )

                # Quitar del thread y de la lista
                info["players"].remove(member)
                await info["thread"].send(f"**{member.display_name}** left")
                try:
                    await info["thread"].remove_user(member)
                except Exception:
                    pass

                # Si la sala quedó vacía, archivar y borrar
                if not info["players"]:
                    try:
                        await info["thread"].edit(archived=True, locked=True)
                        await info["thread"].delete()
                    except Exception:
                        pass
                    self.rooms.pop(rid, None)
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
        season = await self.get_current_season()
        season_id = season["id"]
        view = Top100View(self.db_pool, interaction, season_id)       
        total    = await self.db_pool.fetchval("SELECT COUNT(*) FROM players p inner join seasonplayers sp on p.user_id = sp.user_id where sp.season_id = $1", season_id)
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
    def placement_seed_from_stats(stats) -> tuple[int, str]:
        """
        stats: [perfect, great, good, bad, miss] sumados en las 5 canciones.
        Devuelve (seed_mmr, seed_rank) según la tabla de Placement.
        """
        perfect, great, good, bad, miss = stats
        other = great + good + bad + miss

        if other == 0:                # All perfect (5 songs)
            return 800, "Champion"
        if other == 1:
            return 750, "Master"
        if other == 2:
            return 740, "Master"
        if other == 3:
            return 730, "Master"
        if other == 4:
            return 720, "Master"
        if other == 5:
            return 710, "Master"
        if 6 <= other <= 10:
            return 700, "Master"
        if 11 <= other <= 15:
            return 600, "Crystal"
        if 16 <= other <= 30:
            return 500, "Diamond"
        if 31 <= other <= 70:
            return 400, "Platinum"
        if 71 <= other <= 150:
            return 300, "Gold"
        if 151 <= other <= 500:
            return 200, "Silver"
        if 501 <= other <= 1000:
            return 100, "Bronze"
        return 50, "Iron"  # 1000 - infinito

    async def _resolve_channel(self, channel_id: int, *, fallback: discord.abc.Messageable) -> discord.abc.Messageable:
        ch = self.bot.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            ch = await self.bot.fetch_channel(channel_id)
            return ch
        except Exception:
            # Si no lo encuentro o no tengo permisos, vuelvo al canal actual
            return fallback



    @commands.command(name="submit")
    async def submit(self, ctx: commands.Context, *, block: str):
        mm = self.bot.get_cog("Matchmaking")
        room = next((r for r in mm.rooms.values() if r["thread"].id == ctx.channel.id), None)
        if not room:
            return await ctx.send("Only works inside a room thread")

        # ⛔ sala ya finalizada (no aceptar nuevos submit)
        if room.get("finished", False):
            return await ctx.send("this room has been submited results")

        # ⛔ votación en curso
        if room.get("vote_in_progress", False):
            return await ctx.send("There is already a submit vote in progress in this room.")

        # Debe haberse ejecutado /start
        if room.get("started") is False:
            return await ctx.send("start command need to be excecuted first")

        players = room["players"]
        n = len(players)
        if not (2 <= n <= 5):
            return await ctx.send("Room must have between 2-5 players")

        # ---- Validaciones de formato y pertenencia ----
        expected = room.get("total_notes")
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) != n:
            return await ctx.send(f"You should send {n} lines")

        # Validar formato de cada línea y (si aplica) suma de notas
        players_set = {m.id for m in players}
        ids_in_lines = []
        for idx, ln in enumerate(lines, start=1):
            mobj = ENTRY_RE.match(ln)
            if not mobj:
                return await ctx.send(f"Incorrect format for: {ln}")
            if expected is not None:
                stats = list(map(int, mobj.group("stats").split(",")))
                if sum(stats) != expected:
                    return await ctx.send(
                        f"there is an error on the calculation on line {idx}: {sum(stats)} ≠ {expected}"
                    )
            ids_in_lines.append(int(mobj.group("id")))

        # Duplicados y miembros coincidentes con la sala
        if len(ids_in_lines) != len(set(ids_in_lines)):
            return await ctx.send("❌ Duplicate users in submit.")
        ids_set = set(ids_in_lines)
        missing = players_set - ids_set
        extra   = ids_set - players_set
        if missing or extra:
            msg = []
            if missing:
                msg.append("Missing from submit: " + ", ".join(f"<@{x}>" for x in missing))
            if extra:
                msg.append("Not in the room: " + ", ".join(f"<@{x}>" for x in extra))
            return await ctx.send("❌ Users do not match the room.\n" + "\n".join(msg))

        # A partir de aquí: bloquear nuevos submit mientras dure la votación
        room["vote_in_progress"] = True

        try:
            # ---- Lanzar votación (30s) ----
            intro = (
                f"✅ React ✅ to validate, ❎ to decline (auto-close in {VOTE_TIMEOUT_SUBMIT}s)\n"
                "```\n" + "\n".join(lines) + "\n```"
            )
            vote_msg = await ctx.send(intro)
            for emo in ("✅", "❎"):
                await vote_msg.add_reaction(emo)

            allowed_voters = {m.id for m in players}
            total_voters   = len(allowed_voters)

            counts = {"✅": 0, "❎": 0}
            voted  = set()
            ganador = None

            def check(reaction, user):
                return (
                    reaction.message.id == vote_msg.id
                    and not user.bot
                    and user.id in allowed_voters
                    and str(reaction.emoji) in ("✅", "❎")
                )

            deadline = asyncio.get_event_loop().time() + float(VOTE_TIMEOUT_SUBMIT)

            # Recolectar votos hasta que:
            # - voten todos (y si todos ✅ ⇒ valida; si todos ❎ ⇒ declina), o
            # - se acaben los 30s y decidir por mayoría
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    reaction, user = await self.bot.wait_for("reaction_add", timeout=remaining, check=check)
                except asyncio.TimeoutError:
                    break

                # Un solo voto por usuario
                if user.id in voted:
                    continue

                choice = str(reaction.emoji)
                voted.add(user.id)
                counts[choice] += 1

                # Todos votaron y son unánimes → decisión inmediata
                if len(voted) == total_voters:
                    if counts["✅"] == total_voters:
                        ganador = "✅"
                    elif counts["❎"] == total_voters:
                        ganador = "❎"
                    break

            # Si no hubo unanimidad, decidir por mayoría al cierre
            if ganador is None:
                if counts["✅"] > counts["❎"]:
                    ganador = "✅"
                elif counts["❎"] > counts["✅"]:
                    ganador = "❎"
                else:
                    ganador = "❎"  # empate ⇒ se declina

            # Cerrar visualmente la votación
            try:
                await vote_msg.edit(content=f"🗳️ Voting closed → {ganador}  (✅ {counts['✅']} / ❎ {counts['❎']})")
            except Exception:
                pass

            # ---- Resultado de la votación ----
            if ganador != "✅":
                return await ctx.send("Submit declined. There might be an error, try doing it again.")

            # ==============================
            # VOTO APROBADO → aplicar MMR
            # ==============================
            medals  = {1:"🥇",2:"🥈",3:"🥉"}

            # 1) Mapear datos por user_id
            by_id = {}
            for ln in lines:
                m = ENTRY_RE.match(ln)
                uid   = int(m.group("id"))
                cc    = m.group("cc")
                stats = list(map(int, m.group("stats").split(",")))
                by_id[uid] = (cc, stats)

            # 2) Construir players_list (en el orden lógico de sala)
            players_list = []
            for member in players:
                uid = member.id
                cc, stats = by_id[uid]
                old, current_role = await self.fetch_player(uid)

                # Bandera oficial (DB) como fallback — NO se actualiza aquí
                profile_cc = await self.db_pool.fetchval(
                    "SELECT country FROM players WHERE user_id=$1", uid
                ) or ""

                # Bandera SOLO para el mensaje de resultados (no persistente)
                cc_up = (cc or "").upper()
                display_flag = country_flag(cc_up) or country_flag(profile_cc)

                total_points = sum(s*w for s, w in zip(stats, [4,3,2,1,0]))
                players_list.append({
                    "member": member,
                    "cc": cc,                  # del submit (no persistir)
                    "stats": stats,
                    "total": total_points,
                    "old": old,
                    "role": current_role,
                    "display_flag": display_flag,  # ← usamos esto al imprimir resultados
                })


            # Fase 1: bonus de Placement
            for p in players_list:
                if p["role"] == "Placement":
                    seed_mmr, seed_rank = self.placement_seed_from_stats(p["stats"])
                    p["mmr_actual"] = seed_mmr
                    p["seed_rank"]  = seed_rank
                else:
                    p["mmr_actual"] = p["old"]


            # Fase 2: Elo por pares con K fijo
            players_list.sort(key=lambda x: x["total"], reverse=True)
            ratings = [p["mmr_actual"] for p in players_list]
            order   = list(range(len(players_list)))
            deltas  = self.pairwise_deltas_simple(ratings, order, K=30, scale=400, cap=39)

            summary = []
            for idx, p in enumerate(players_list, start=1):
                mmr_prev  = int(p["mmr_actual"])
                mmr_delta = int(deltas[idx-1])
                mmr_final = mmr_prev + mmr_delta
                role_name = get_rank_from_mmr(mmr_final)

                season_row  = await self.get_current_season()
                season_id   = season_row["id"]

                # Asegura la tabla con constraint único (solo una vez; puedes moverlo a cog_load si prefieres)
                await self.db_pool.execute("""
                    CREATE TABLE IF NOT EXISTS seasonplayers (
                        season_id   INTEGER NOT NULL,
                        user_id     BIGINT  NOT NULL,
                        mmr         INTEGER NOT NULL DEFAULT 0,
                        role        TEXT    NOT NULL DEFAULT 'Placement',
                        PRIMARY KEY (season_id, user_id)
                    );
                """)

                # UPSERT por temporada
                await self.db_pool.execute(
                    """
                    INSERT INTO seasonplayers (season_id, user_id, mmr, role)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (season_id, user_id)
                    DO UPDATE SET mmr = EXCLUDED.mmr, role = EXCLUDED.role
                    """,
                    season_id, p["member"].id, mmr_final, role_name
                )

                # Actualización de roles en Discord
                role_id = RANK_ROLE_IDS.get(role_name)
                if role_id:
                    role_obj = ctx.guild.get_role(role_id)
                    if role_obj:
                        old_ranks  = set(RANK_ROLE_IDS.values()) | {PLACEMENT_ROLE_ID}
                        keep_roles = [r for r in p["member"].roles if r.id not in old_ranks]
                        new_roles  = list(dict.fromkeys(keep_roles + [role_obj]))
                        try:
                            await p["member"].edit(roles=new_roles, reason="Rank update")
                        except (discord.Forbidden, discord.HTTPException) as e:
                            await ctx.send(f"⚠️ No pude actualizar roles de {p['member'].mention}: {e}")

                # Opcional: actualizar nick con el rango
                if ctx.guild.me.guild_permissions.manage_nicknames:
                    base = p["member"].display_name
                    nick = f"{base} [{role_name}]"
                    if len(nick) > 32:
                        nick = f"{base[:max(0, 32 - len(role_name) - 3)]} [{role_name}]"
                    try:
                        await p["member"].edit(nick=nick)
                    except (discord.Forbidden, discord.HTTPException) as e:
                        await ctx.send(f"⚠️ No pude cambiar nick de {p['member'].mention}: {e}")

                # ← medalla / posición: usa 🥇🥈🥉 para top3 y número para el resto
                pos_str = medals.get(idx) or f"{idx}."

                name_with_flag = f"{p['member'].display_name} {p['display_flag']}".rstrip()
                summary.append((
                    pos_str,
                    name_with_flag,
                    p["total"],
                    p["stats"],
                    mmr_prev,
                    mmr_delta,
                    mmr_final
                ))


            # Publicar resultados en canal de resultados y en el hilo (como board)
            season_row    = await self.get_current_season()
            season_name   = season_row["name"] if season_row else "N/A"
            join_parent = ctx.channel.parent
            result_chan_id = JOIN_TO_RESULTS.get(join_parent.id) if join_parent else None
            result_chan = await self._resolve_channel(result_chan_id, fallback=ctx.channel) if result_chan_id else ctx.channel

            board_block = self._format_results_board(summary)
            embed = discord.Embed(
                title=f"RESULTS — {season_name}",
                description=board_block,
                color=0xff0000
            )
            await result_chan.send(embed=embed)
            await ctx.send(embed=embed)
            await result_chan.send("MMR Updated")
            await ctx.send("MMR Updated")


            # Bloquear futuros submit en esta sala y permitir /c en otra
            room["closed"] = True           # ya no acepta entradas nuevas
            room["finished"] = True         # _find_player_room_id ahora la ignora para el bloqueo de /c

            await ctx.send("This room has been submited results")

            # Notificar al resto de cogs (por ejemplo, Rooms) que la sala terminó
            rid = next((rid for rid, r in self.rooms.items() if r["thread"].id == ctx.channel.id), None)
            if rid is not None:
                self.bot.dispatch('room_finished', rid)

            # Programar el cierre/borrado en segundo plano
            asyncio.create_task(self.close_room(ctx.channel, delay=60, delete=True))


        finally:
            # Siempre liberar el bloqueo de votación
            room["vote_in_progress"] = False



async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))
 #ed