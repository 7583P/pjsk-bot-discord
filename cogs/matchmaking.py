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

# ————— Append: solo limita el rango de selección para canciones 'append' —————
APPEND_RANKS_ORDER = [
    "Legend",
    "Grand Champion",
    "Champion",
    "Master ⬆",
    "Master",
    "Crystal ⬆",
    "Crystal",
    "Diamond ⬆",
    "Diamond",
]


APPEND_RANGES = {
    "Diamond":        (24, 27),
    "Diamond ⬆":      (25, 28),
    "Crystal":        (26, 29),
    "Crystal ⬆":      (27, 30),
    "Master":         (28, 31),
    "Master ⬆":       (29, 32),
    "Champion":       (30, 33),
    "Grand Champion": (31, 35),
    "Legend":         (32, 37),
}

# Tamaño del pool para non-append
NON_APPEND_POOL_LIMIT = 10

# Cuántas canciones append se meten al pool según el rango más alto presente
APPEND_POOL_CONFIG = {
    "Diamond":        5,  # ~500 MMR
    "Diamond ⬆":      5,  # ~550 MMR
    "Crystal":        5,  # ~600
    "Crystal ⬆":      5,  # ~650
    "Master":         5,  # ~700
    "Master ⬆":       5,  # ~750
    "Champion":       5,  # ~800
    "Grand Champion": 5,  # ~900
    "Legend":         5,  # 1000+
}

# Probabilidad de usar append según el rango más alto presente
APPEND_PROBABILITIES = {
    "Diamond":        0.10,
    "Diamond ⬆":        0.15,
    "Crystal":        0.20,
    "Crystal ⬆":        0.25,
    "Master":         0.30,
    "Master ⬆":         0.35,
    "Champion":       0.40,
    "Grand Champion": 0.45,
    "Legend":         0.50,
}

def get_append_probability_for_rank(rank: str) -> float:
    """Devuelve la probabilidad de habilitar canciones append para el rango dado."""
    return APPEND_PROBABILITIES.get(rank, 0.0)


def get_append_pool_limit_for_rank(rank: str) -> int:
    """Devuelve cuántas canciones append meter al pool según el rango alto."""
    return APPEND_POOL_CONFIG.get(rank, 0)


def _highest_high_rank(counts: dict[str, int]) -> str | None:
    """Devuelve el rango más alto presente entre Diamond..Legend, o None si no hay."""
    for r in APPEND_RANKS_ORDER:
        if counts.get(r, 0) > 0:
            return r
    return None


# Prioridad global de dificultades, ahora incluyendo hard
DIFFICULTY_PRIORITY = ("hard", "expert", "master", "append")

# Mapeo de rangos a dificultades permitidas
ALLOWED_DIFFS_BY_RANK = {
    "Placement":     ("expert",),
    "Iron":          ("hard", "expert", "master"),
    "Bronze":        ("hard", "expert", "master"),
    "Bronze ⬆":      ("hard", "expert", "master"),
    "Silver":        ("hard", "expert", "master"),
    "Silver ⬆":      ("hard", "expert", "master"),
    "Gold":          ("hard", "expert", "master"),
    "Gold ⬆":        ("hard", "expert", "master"),
    "Platinum":      ("hard", "expert", "master"),
    "Platinum ⬆":      ("hard", "expert", "master"),
    "Diamond":       ("expert", "master", "append"),
    "Diamond ⬆":     ("expert", "master", "append"),
    "Crystal":       ("expert", "master", "append"),
    "Crystal ⬆":     ("expert", "master", "append"),
    "Master":        ("expert", "master", "append"),
    "Master ⬆":    ("expert", "master", "append"),
    "Champion":      ("expert", "master", "append"),
    "Grand Champion": ("expert", "master", "append"),
    "Legend":        ("expert", "master", "append"),
}

# Nuevos rangos con gap exacto
BRACKET_RANGES = {
    "Placement":     (24, 28),
    "Iron":          (17, 22),
    "Bronze":        (18, 23),
    "Bronze ⬆":      (19, 24),
    "Silver":        (20, 25),
    "Silver ⬆":      (21, 26),
    "Gold":          (22, 27),
    "Gold ⬆":        (23, 28),
    "Platinum":      (24, 29),
    "Platinum ⬆":    (25, 29),
    "Diamond":       (26, 30),
    "Diamond ⬆":     (27, 30), 
    "Crystal":       (28, 31),
    "Crystal ⬆":     (28, 32),
    "Master":        (29, 33),
    "Master ⬆":      (30, 34),
    "Champion":      (31, 34),
    "Grand Champion": (31, 35),
    "Legend":        (32, 37),
}

MIN_LEVEL_GAP = 3

VOTE_TIMEOUT_SUBMIT = 30 
VOTE_TIMEOUT_FORFEIT = 30

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
    elif mmr <= 149:
        return "Bronze"
    elif mmr <= 199:
        return "Bronze ⬆"
    elif mmr <= 249:
        return "Silver"
    elif mmr <= 299:
        return "Silver ⬆"
    elif mmr <= 349:
        return "Gold"
    elif mmr <= 399:
        return "Gold ⬆"
    elif mmr <= 449:
        return "Platinum"
    elif mmr <= 499:
        return "Platinum ⬆"
    elif mmr <= 549:
        return "Diamond"
    elif mmr <= 599:
        return "Diamond ⬆"
    elif mmr <= 649:
        return "Crystal"
    elif mmr <= 699:
        return "Crystal ⬆"
    elif mmr <= 749:
        return "Master"
    elif mmr <= 799:
        return "Master ⬆"
    elif mmr <= 899:
        return "Champion"
    elif mmr <= 999:
        return "Grand Champion"
    else:
        return "Legend"

RANK_ROLE_IDS = {
    "Placement":    1371321594068336811,
    "Iron":        1394444407536881845,        
    "Bronze":        1371324225838645339,
    "Bronze ⬆":        1433827677713858732,
    "Silver":        1389343997100560514,
    "Silver ⬆":        1433827565222494383,
    "Gold":          1371324328708149328,
    "Gold ⬆":          1433827466744561720,
    "Platinum":      1389343805521789098,
    "Platinum ⬆":    1434564540619554960,
    "Diamond":      1371324561542484108,
    "Diamond ⬆":    1440477553151184916,
    "Crystal":       1394445724703527012,
    "Crystal ⬆":   1440477788501966858,
    "Master":       1394635883466199110,
    "Master ⬆":   1440478019012530267,
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
    
    async def _get_room_rank_by_avg_mmr(self, players: list[discord.Member]) -> str:
        """
        Usa el MMR promedio del room para mapearlo a un rango con get_rank_from_mmr.
        """
        if not players:
            return "Diamond"  # fallback neutro

        mmrs = []
        for m in players:
            mmr_val, _ = await self.fetch_player(m.id)
            mmrs.append(mmr_val)

        avg_mmr = round(sum(mmrs) / len(mmrs)) if mmrs else 0
        return get_rank_from_mmr(avg_mmr)

    
    async def _get_random_songs(
        self,
        low: int,
        high: int,
        diffs: tuple[str, ...],
        limit: int,
        *,
        server: str = "en",
        exclude_titles: set[str] | None = None
    ):
        """
        Devuelve hasta 'limit' canciones aleatorias (title, level, diff_capitalized, notes)
        en [low, high] y diffs dadas, filtrando por server ('en' o 'jp').
        """
        exclude_titles = exclude_titles or set()
        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                placeholders = ",".join(f"${i+4}" for i in range(len(diffs)))
                params: list = [low, high, server, *diffs]
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
                      AND server = $3
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


    async def _pick_placement_songs_levels_24_to_28(
        self,
        *,
        server: str = "en"
    ) -> list[tuple[str, int, str, int]]:
        """
        Regla especial Placement:
        - EXACTAMENTE 1 canción por nivel: 24, 25, 26, 27 y 28
        - Solo EXPERT
        - Evita repetir títulos
        - Si un nivel no tiene canciones Expert en el catálogo, intenta fallback aleatorio dentro 24–28.
        Filtra por server ('en' o 'jp').
        """
        levels = [24, 25, 26, 27, 28]
        picks: list[tuple[str, int, str, int]] = []
        chosen_titles: set[str] = set()

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                for L in levels:
                    rows = await conn.fetch(
                        """
                        SELECT title, level, diff, notes_count
                        FROM songs
                        WHERE level = $1
                          AND diff IN ('expert')
                          AND server = $2
                        ORDER BY random()
                        LIMIT 5
                        """,
                        L, server
                    )

                    picked = None
                    for r in rows:
                        if r["title"] not in chosen_titles:
                            picked = r
                            break

                    if picked is None:
                        fb = await conn.fetch(
                            """
                            SELECT title, level, diff, notes_count
                            FROM songs
                            WHERE level BETWEEN 24 AND 28
                              AND diff IN ('expert')
                              AND server = $1
                            ORDER BY random()
                            LIMIT 10
                            """,
                            server
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
        for pos, name_with_flag, pts, stats, mmr_prev, mmr_delta, mmr_final, was_placement in summary_rows:
            # Convierte 'pe', 'jp', 'us' finales → 🇵🇪 🇯🇵 🇺🇸
            safe_name = flagify_iso2_suffix(name_with_flag)

            pggbm = f"({stats[0]},{stats[1]},{stats[2]},{stats[3]},{stats[4]})"

            if was_placement:
                # PLACEMENT(mmr_inicial) +X = Y
                mmr_display = f"PLACEMENT({mmr_prev}) {mmr_delta:+3d} = {mmr_final}"
            else:
                mmr_display = f"{mmr_prev:>4} {mmr_delta:+3d} = {mmr_final:<4}"

            lines.append(
                f"{str(pos):<{W_POS}}| "
                f"{safe_name:<{W_PLAYER}}| "
                f"{pggbm:<{W_PGGBM}}| "
                f"{pts:>{W_POINTS}}| "
                f"{mmr_display}"
            )



        body = "\n".join(lines) if lines else "(sin filas)"
        # Solo retorna el bloque monoespaciado (sin título). El título irá en el Embed.
        return "```\n" + colh + "\n" + sep + "\n" + body + "\n```"

    async def _resolve_season_id(self, season_choice: str | None) -> tuple[int | None, str]:
        """
        Devuelve (season_id, season_name) según la elección del usuario.
        - None o "current/actual" => temporada activa (get_current_season)
        - "AlphaSeason"                  => busca por nombre 'AlphaSeason' (case-insensitive)
        - "Beta"             => busca por 'Beta'/'Beta'/'Beta' (case-insensitive)
        - Si no coincide, intenta buscar por nombre exacto (case-insensitive) o por ID numérico.
        """
        val = (season_choice or "current").strip()
        key = val.lower()

        # current / actual → temporada activa
        if key in ("current", "actual"):
            row = await self.get_current_season()
            return (row["id"], row["name"]) if row else (None, "N/A")

        # Mapas de alias por conveniencia
        aliases = {
            "alphaseason": ["alphaseason"],
            "beta": ["beta", "beta", "beta"],
        }
        candidate_names = aliases.get(key, [val])

        async with self.db_pool.acquire() as conn:
            # Intento por nombres (case-insensitive), priorizando la más reciente
            where = " OR ".join(f"LOWER(name) = ${i+1}" for i in range(len(candidate_names)))
            row = await conn.fetchrow(
                f"SELECT id, name FROM seasons WHERE {where} ORDER BY startdate DESC LIMIT 1",
                *[c.lower() for c in candidate_names]
            )

            if not row:
                # Si el usuario pasó un ID numérico
                if key.isdigit():
                    row = await conn.fetchrow(
                        "SELECT id, name FROM seasons WHERE id = $1",
                        int(key)
                    )

        return (row["id"], row["name"]) if row else (None, val)


    async def _fetch_user_stats(self, user_id: int, season_id: int):
        """
        Devuelve un dict con:
        - pos, total, mmr, role             (de seasonplayers)
        - matches_played                    (COUNT(*) de match_history)
        - last10_wins, last10_losses        (W-L Last 10)
        - last10_mmr_delta                  (suma de mmr_delta en las últimas 10 partidas)
        - peak_mmr                          (máximo mmr_after de la season)
        Todo filtrado por season_id, así sirve para cualquier season.
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
        SELECT r.pos,
               (SELECT COUNT(*) FROM seasonplayers WHERE season_id = $2) AS total,
               r.mmr,
               r.role
        FROM ranked r
        WHERE r.user_id = $1
        """

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(sql, user_id, season_id)
            if not row:
                return None

            pos   = int(row["pos"])
            total = int(row["total"])
            mmr   = int(row["mmr"])
            role  = row["role"]

            # Total de partidas jugadas en la season
            matches_played = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                """,
                season_id, user_id
            ) or 0

            # Últimas 10 partidas (o menos si no hay tantas)
            recent_rows = await conn.fetch(
                """
                SELECT is_win, mmr_delta
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                ORDER BY played_at DESC, id DESC
                LIMIT 10
                """,
                season_id, user_id
            )

            last10_wins   = sum(1 for r in recent_rows if r["is_win"])
            last10_losses = sum(1 for r in recent_rows if not r["is_win"])
            last10_delta  = sum(int(r["mmr_delta"]) for r in recent_rows) if recent_rows else 0

            # Peak MMR dentro de la season (máximo mmr_after)
            peak_mmr = await conn.fetchval(
                """
                SELECT MAX(mmr_after)
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                """,
                season_id, user_id
            )
            if peak_mmr is None:
                peak_mmr = mmr  # si no tiene histórico, usamos el MMR actual

        return {
            "pos": pos,
            "total": total,
            "mmr": mmr,
            "role": role,
            "matches_played": matches_played,
            "last10_wins": last10_wins,
            "last10_losses": last10_losses,
            "last10_mmr_delta": last10_delta,
            "peak_mmr": int(peak_mmr),
        }



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

    async def _fetch_cutoff(self, season_id: int, top_n: int) -> int | None:
        """
        Devuelve el MMR del jugador en la posición `top_n` (T10, T20, etc.)
        para la temporada indicada. Si no hay suficientes jugadores devuelve None.
        """
        assert top_n >= 1

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT mmr
                FROM seasonplayers
                WHERE season_id = $1
                ORDER BY mmr DESC
                OFFSET $2
                LIMIT 1
                """,
                season_id,
                top_n - 1,          # OFFSET es 0-based
            )

        return int(row["mmr"]) if row else None


    def _format_cutoffs_board(self, season_name: str, data: dict[int, int | None]) -> str:
        """
        data: {10: mmr_T10, 20: mmr_T20, ...}
        Devuelve un string listo para enviar en Discord con formato tipo tabla.
        """
        lines: list[str] = []
        lines.append(f"MMR cutoffs — Season {season_name}")
        lines.append("```")
        lines.append("Tier | Players |  MMR")
        lines.append("-----+---------+------")

        # Ordenar T10, T20, T30, ...
        for n in sorted(data.keys()):
            mmr = data[n]
            tier_label = f"T{n}"
            mmr_str = str(mmr) if mmr is not None else "N/A"
            lines.append(f"{tier_label:<4}| {n:>7} | {mmr_str:>6}")

        lines.append("```")
        return "\n".join(lines)



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

            # NEW: tabla de settings globales (para el contador de results)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS global_settings (
                    key   TEXT PRIMARY KEY,
                    value BIGINT NOT NULL
                );
                """
            )

            # NEW: tabla de resultados, indexados por un ID global que NO se resetea
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    result_id   BIGINT PRIMARY KEY,
                    season_id   INTEGER NOT NULL,
                    season_name TEXT    NOT NULL,
                    channel_id  BIGINT  NOT NULL,
                    message_id  BIGINT  NOT NULL,
                    gap_low     INTEGER,
                    gap_high    INTEGER,
                    room_mmr    INTEGER,               -- NEW
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # Historial de partidas por jugador y temporada
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS match_history (
                    id            BIGSERIAL PRIMARY KEY,
                    season_id     INTEGER       NOT NULL,
                    result_id     BIGINT,             -- NEW: id global del result
                    user_id       BIGINT        NOT NULL,
                    pos           INTEGER,            -- NEW: posición en el result
                    s_perfect     INTEGER,            -- NEW: P
                    s_great       INTEGER,            -- NEW: G
                    s_good        INTEGER,            -- NEW: G2
                    s_bad         INTEGER,            -- NEW: B
                    s_miss        INTEGER,            -- NEW: M
                    mmr_before    INTEGER       NOT NULL,
                    mmr_after     INTEGER       NOT NULL,
                    mmr_delta     INTEGER       NOT NULL,
                    was_placement BOOLEAN       NOT NULL DEFAULT false,
                    is_win        BOOLEAN       NOT NULL,
                    played_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
                );
            """)

            # Por si ya existía tabla vieja:
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS result_id BIGINT;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS pos INTEGER;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_perfect INTEGER;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_great INTEGER;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_good INTEGER;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_bad INTEGER;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_miss INTEGER;")
            await conn.execute("ALTER TABLE match_history ADD COLUMN IF NOT EXISTS was_placement BOOLEAN DEFAULT false;")

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_match_history_user_season
                ON match_history (user_id, season_id, played_at DESC);
            """)


            # Canciones (catálogo global). Si ya existe, no re-crea.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS songs (
                    id           INTEGER NOT NULL,
                    title        TEXT    NOT NULL,
                    diff         TEXT    NOT NULL,
                    level        INTEGER NOT NULL,
                    notes_count  INTEGER NOT NULL DEFAULT 0
                    -- OJO: sin server aquí o con server, da igual
                    -- si ya existe no toca la tabla.
                );
            """)

            # 1️⃣ Primero aseguro que la columna server exista
            await conn.execute("""
                ALTER TABLE songs
                ADD COLUMN IF NOT EXISTS server TEXT NOT NULL DEFAULT 'en';
            """)

            # 2️⃣ Ahora sí puedo borrar duplicados por (id,diff,server)
            await conn.execute("""
                DELETE FROM songs a
                USING songs b
                WHERE a.id = b.id
                  AND a.diff = b.diff
                  AND a.server = b.server
                  AND a.ctid > b.ctid;
            """)

            # 3️⃣ Unique en (id, diff, server) para EN/JP
            await conn.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1
                        FROM   pg_constraint
                        WHERE  conname = 'songs_id_diff_key'
                        AND    conrelid = 'songs'::regclass
                    ) THEN
                        ALTER TABLE songs
                        DROP CONSTRAINT songs_id_diff_key;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1
                        FROM   pg_constraint
                        WHERE  conname = 'songs_id_diff_server_key'
                        AND    conrelid = 'songs'::regclass
                    ) THEN
                        ALTER TABLE songs
                        ADD CONSTRAINT songs_id_diff_server_key UNIQUE (id, diff, server);
                    END IF;
                END$$;
            """)

            # Índices útiles
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_level_server ON songs(level, server);")




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

                bundles: list[tuple[str, list[dict], list[dict]]] = []

                # EN
                try:
                    musics_en = await grab(self.RAW_EN, "musics")
                    diffs_en  = await grab(self.RAW_EN, "musicDifficulties")
                    bundles.append(("en", musics_en, diffs_en))
                except Exception as e:
                    log.warning(f"[songs] EN fetch failed ({e})")

                # JP
                try:
                    musics_jp = await grab(self.RAW_JP, "musics")
                    diffs_jp  = await grab(self.RAW_JP, "musicDifficulties")
                    bundles.append(("jp", musics_jp, diffs_jp))
                except Exception as e:
                    log.warning(f"[songs] JP fetch failed ({e})")

                if not bundles:
                    log.error("[songs] No se pudo obtener ni EN ni JP.")
                    return

            now_ts = int(datetime.datetime.utcnow().timestamp())

            # agrupamos filas por servidor, para poder reemplazar por servidor
            by_server: dict[str, list[tuple[int, str, str, int, int, str]]] = {}

            for server_code, musics, diffs in bundles:
                music_by_id = {m["id"]: m for m in musics}

                rows_for_server: list[tuple[int, str, str, int, int, str]] = []

                for d in diffs:
                    if d.get("musicDifficulty") not in self.DIFFS:
                        continue

                    music = music_by_id.get(d["musicId"], {})
                    title = music.get("title", f"ID {d['musicId']}")

                    # Fecha de release del servidor correspondiente
                    raw_release = (
                        music.get("publishedAt")
                        or music.get("startAt")
                        or music.get("releaseAt")
                        or 0
                    )

                    # normalizamos a segundos
                    if raw_release and raw_release > 10_000_000_000:
                        release_ts = int(raw_release // 1000)
                    else:
                        release_ts = int(raw_release or 0)

                    # 🔴 REGLA NUEVA:
                    #  - si NO hay fecha (release_ts == 0) → asumimos que NO está en el juego todavía → no la usamos
                    #  - si todavía no ha pasado 1 día desde el release → tampoco la usamos
                    if release_ts == 0:
                        continue

                    if (release_ts + 24 * 60 * 60) > now_ts:
                        # aún no ha pasado 1 día desde el release
                        continue

                    # Cálculo robusto de notas
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

                    rows_for_server.append((
                        d["musicId"],
                        title,
                        d["musicDifficulty"],
                        int(d["playLevel"]),
                        int(notes_total or 0),
                        server_code,      # 'en' o 'jp'
                    ))

                by_server[server_code] = rows_for_server

            async with self.db_pool.acquire() as conn:
                # Solo tocamos los servidores que sí pudimos refrescar en este ciclo
                for server_code, rows in by_server.items():
                    # Limpiamos catálogo viejo de ese servidor
                    await conn.execute(
                        "DELETE FROM songs WHERE server = $1",
                        server_code
                    )

                    if not rows:
                        log.warning(f"[songs] No hay canciones válidas para server={server_code} tras filtrar por release+1d.")
                        continue

                    # Insertamos solo las canciones que YA cumplieron el día de gracia
                    await conn.executemany(
                        """
                        INSERT INTO songs (id, title, diff, level, notes_count, server)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        rows
                    )

        print("[songs] Catálogo EN/JP actualizado (reemplazo por servidor, con filtro release+1d)")



    @refresh_songs.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _get_10_songs(self, low, high, diffs=None):
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

        # Mezcla global y recorta a 10
        random.shuffle(picks)
        return picks[:10]




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

        server_code = room_info.get("server", "en")

        # 1) Contar rangos (por rol primero, luego por MMR)
        counts = {rank: 0 for rank in BRACKET_RANGES}
        for m in players:
            # 1) Intentar detectar rango por rol
            rank_by_role = next(
                (r for r in BRACKET_RANGES if discord.utils.get(m.roles, name=r)),
                None
            )

            if rank_by_role is not None:
                # Si tiene un rol de rango, usamos ese (Placement solo si tiene el rol)
                user_rank = rank_by_role
            else:
                # 2) Sin rol de rango → usar rango según MMR (NO se considera Placement)
                mmr_val, _ = await self.fetch_player(m.id)
                user_rank = get_rank_from_mmr(mmr_val)

            counts[user_rank] = counts.get(user_rank, 0) + 1

        # 2) Determinar rango base según MMR promedio del room
        has_placement = counts.get("Placement", 0) > 0
        if has_placement:
            base_rank = "Placement"
        else:
            # usa el MMR promedio de la sala para determinar el rango
            base_rank = await self._get_room_rank_by_avg_mmr(players)
            if base_rank not in BRACKET_RANGES:
                base_rank = "Diamond"  # fallback neutro por si acaso

        low, high = BRACKET_RANGES[base_rank]
        allowed_diffs = ALLOWED_DIFFS_BY_RANK.get(base_rank, DIFFICULTY_PRIORITY)

        # 3) Selección de canciones
        #    - si la sala es Placement → modo especial 24–28 EXPERT
        #    - en otro caso → gap base y append también según base_rank (MMR promedio)
        if base_rank == "Placement":
            # Regla especial de Placement: 1 canción por nivel 24–28, solo EXPERT
            picks = await self._pick_placement_songs_levels_24_to_28(server=server_code)
            low, high = 24, 28
        else:
            # a) gap base para non-append (según rango por MMR promedio)
            base_lo, base_hi = low, high
            non_append_diffs = tuple(d for d in allowed_diffs if d != "append")

            # b) Determinar (opcional) el gap de append usando el MISMO rango del room (avg MMR)
            append_lo_hi = None
            append_limit = 0
            room_rank_for_append = base_rank  # ya es el rango basado en avg MMR
            if "append" in allowed_diffs:
                append_lo_hi = APPEND_RANGES.get(room_rank_for_append)
                append_limit = get_append_pool_limit_for_rank(room_rank_for_append)
                prob         = get_append_probability_for_rank(room_rank_for_append)

                # si no hay rango o la tirada de prob falla → no usar append
                if not append_lo_hi or append_limit <= 0 or random.random() >= prob:
                    append_lo_hi = None
                    append_limit = 0

            # c) si vamos a usar append y su rango sale del base, EXPANDIR el base
            if append_lo_hi is not None and append_limit > 0:
                alo, ahi = append_lo_hi
                base_lo = min(base_lo, alo)
                base_hi = max(base_hi, ahi)

            # d) Traer canciones por tipo de diff
            pool = []

            # Non-append (hard/expert/master) en gap base (posiblemente expandido)
            if non_append_diffs:
                non_append_pool = await self._get_random_songs(
                    base_lo, base_hi, non_append_diffs,
                    limit=NON_APPEND_POOL_LIMIT,
                    server=server_code,
                )
                pool.extend(non_append_pool)
                taken_titles = {t for (t, *_) in non_append_pool}
            else:
                taken_titles = set()

            # Append en su propio rango (también basado en avg MMR)
            if append_lo_hi is not None and append_limit > 0:
                alo, ahi = append_lo_hi
                append_pool = await self._get_random_songs(
                    alo, ahi, ("append",),
                    limit=append_limit,
                    server=server_code,
                    exclude_titles=taken_titles,
                )
                pool.extend(append_pool)

            # e) Mezclar y tomar 5
            random.shuffle(pool)
            picks = pool[:5]

            # f) El rango mostrado es SIEMPRE el base final (ya expandido)
            low, high = base_lo, base_hi

        # 4) Construir el mensaje
        total_notes = sum(int(n or 0) for *_, n in picks)  # suma segura

        # Guardar info de la sala
        room_info["total_notes"] = total_notes
        room_info["gap_low"]     = low
        room_info["gap_high"]    = high

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

        # Solo funciona dentro de un hilo cuyo padre sea un #join permitido
        if not (isinstance(ch, Thread) and ch.parent_id in ALLOWED_JOIN_CHANNELS):
            return await interaction.response.send_message(
                "This command only works in rooms",
                ephemeral=True
            )

        thread = ch

        # Buscar la sala asociada a este hilo
        room_entry = next(
            (r for r in self.rooms.values() if r["thread"].id == thread.id),
            None
        )
        if not room_entry:
            return await interaction.response.send_message(
                "⚠️ No encuentro la sala asociada a este hilo.",
                ephemeral=True
            )

        # Ya se inició en esta sala
        if room_entry.get("started"):
            return await interaction.response.send_message(
                "songs has been selected",
                ephemeral=True
            )

        # Jugadores actuales (sin bots)
        players = [m for m in room_entry["players"] if not m.bot]

        # Validar 3–5 jugadores
        if not (3 <= len(players) <= 5):
            return await interaction.response.send_message(
                "Room must have between 3-5 players",
                ephemeral=True
            )

        # Aseguramos que room_entry["players"] coincide con la lista filtrada
        room_entry["players"] = players

        # Reutiliza TODA la lógica de selección de canciones aquí
        await self.launch_song_poll(room_entry)

        # Confirmación efímera al invocador
        await interaction.response.send_message(
            f"✅ Votación iniciada en {thread.mention}",
            ephemeral=True
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

        # Determinar server en función del canal #join
        if ch.id == 1378215330979254402:   # jp-pjsk → #join JP
            server_code = "jp"
        else:
            server_code = "en"

        mmr_val, _ = await self.fetch_player(member.id)

        # Buscar sala NO cerrada cuyo padre sea EXACTAMENTE este canal #join
        best_rid = None
        for rid, info in self.rooms.items():
            thread_parent_id = info["thread"].parent_id
            if (
                thread_parent_id == ch.id
                and not info.get("closed", False)
                and not info.get("started", False)
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
                "category_id": ch.category_id or 0,
                "closed": False,
                "started": False,
                "finished": False,
                "server": server_code,   # 👈 EN o JP, se guarda en la sala
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
    @app_commands.describe(season="Season: Current, AlphaSeason or Beta")
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Beta",   value="beta"),
            app_commands.Choice(name="AlphaSeason",   value="alphaseason"),
        ]
    )
    async def stats(self, interaction: discord.Interaction, season: app_commands.Choice[str] | None = None):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message("⚠️ Season not found", ephemeral=True)

        user_id = interaction.user.id
        data = await self._fetch_user_stats(user_id, season_id)
        if not data:
            return await interaction.response.send_message(
                f"You dont have register in **Season {season_name}**.", ephemeral=True
            )

        mmr_delta_last10 = data["last10_mmr_delta"]
        mmr_delta_str = f"{mmr_delta_last10:+d}"

        msg = (
            f"**Stats — Season {season_name}**\n"
            f"• **Player:** {interaction.user.mention}\n"
            f"• **Position:** #{data['pos']} of {data['total']}\n"
            f"• **MMR:** {data['mmr']}\n"
            f"• **Rank:** {data['role']}\n"
            f"• **Matches Played:** {data['matches_played']}\n"
            f"• **W-L Last 10:** {data['last10_wins']}-{data['last10_losses']}\n"
            f"• **W-L Last 10 (±MMR):** {mmr_delta_str}\n"
            f"• **Peak MMR:** {data['peak_mmr']}\n"
        )
        await interaction.response.send_message(msg)


    @app_commands.command(
        name="searchresult",
        description="Show a past results table by its global ID"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(result_id="Result ID shown under the season name in the results embed")
    async def searchresult(self, interaction: discord.Interaction, result_id: int):
        # Buscar el result en la tabla
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT result_id, season_name, channel_id, message_id, gap_low, gap_high, created_at
                FROM results
                WHERE result_id = $1
                """,
                result_id
            )

        if not row:
            return await interaction.response.send_message(
                f"⚠️ No result found with ID `{result_id}`.",
                ephemeral=True
            )

        channel = await self._resolve_channel(row["channel_id"], fallback=interaction.channel)
        try:
            msg = await channel.fetch_message(row["message_id"])
        except discord.NotFound:
            return await interaction.response.send_message(
                "⚠️ The original result message could not be found (maybe deleted).",
                ephemeral=True
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⚠️ I don't have permissions to view the channel of that result.",
                ephemeral=True
            )

        content = (
            f"Result **#{row['result_id']}** — **{row['season_name']}**\n"
            f"[Jump to original message]({msg.jump_url})"
        )

        if msg.embeds:
            await interaction.response.send_message(
                content,
                embed=msg.embeds[0],
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                content,
                ephemeral=True
            )


    @app_commands.command(name="stats_user", description="Shows your position, mmr and rank in the selected season")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(user="Player", season="Season: Current, AlphaSeason or Beta")
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Beta",   value="beta"),
            app_commands.Choice(name="AlphaSeason",   value="alphaseason"),
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

        mmr_delta_last10 = data["last10_mmr_delta"]
        mmr_delta_str = f"{mmr_delta_last10:+d}"

        msg = (
            f"**Stats — Season {season_name}**\n"
            f"• **Player:** {user.mention}\n"
            f"• **Position:** #{data['pos']} of {data['total']}\n"
            f"• **MMR:** {data['mmr']}\n"
            f"• **Rank:** {data['role']}\n"
            f"• **Matches Played:** {data['matches_played']}\n"
            f"• **W-L Last 10:** {data['last10_wins']}-{data['last10_losses']}\n"
            f"• **W-L Last 10 (±MMR):** {mmr_delta_str}\n"
            f"• **Peak MMR:** {data['peak_mmr']}\n"
        )
        await interaction.response.send_message(msg)


    @app_commands.command(
        name="cutoffs",
        description="Shows the MMR Cutoff of T10, T20, T30, T40, T50 y T100 for the selected season"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(season="Temporada: Current, AlphaSeason or Beta")
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Beta", value="beta"),
            app_commands.Choice(name="AlphaSeason", value="alphaseason"),
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
        songs = await self._get_10_songs(1, 99)
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

    # NEW: contador global de resultados (0,1,2,3,...) que NO se resetea por season
    async def _next_result_id(self) -> int:
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM global_settings WHERE key = 'last_result_id'"
            )
            if not row:
                next_id = 0
                await conn.execute(
                    """
                    INSERT INTO global_settings(key, value)
                    VALUES ('last_result_id', $1)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    next_id
                )
            else:
                next_id = int(row["value"]) + 1
                await conn.execute(
                    "UPDATE global_settings SET value = $1 WHERE key = 'last_result_id'",
                    next_id
                )
        return next_id


    @staticmethod
    def placement_seed_from_stats(stats) -> tuple[int, str]:
        """
        stats: [perfect, great, good, bad, miss] sumados en las 5 canciones.
        Devuelve (seed_mmr, seed_rank) según la tabla de Placement.
        """
        perfect, great, good, bad, miss = stats
        other = great + good + bad + miss

        if other == 0:  # All perfect (5 songs)
            return 800, "Champion"
        if other == 1:
            return 790, "Master ⬆"
        if other == 2:
            return 780, "Master ⬆"
        if other == 3:
            return 770, "Master ⬆"
        if other == 4:
            return 760, "Master ⬆"
        if other == 5:
            return 750, "Master ⬆"
        if other == 6:
            return 740, "Master"
        if other == 7:
            return 730, "Master"
        if other == 8:
            return 720, "Master"
        if other == 9:
            return 710, "Master"
        if other == 10:
            return 700, "Master"
        if other == 11:
            return 690, "Crystal ⬆"
        if other == 12:
            return 680, "Crystal ⬆"
        if other == 13:
            return 670, "Crystal ⬆"
        if other == 14:
            return 660, "Crystal ⬆"
        if other == 15:
            return 650, "Crystal ⬆"
        if other == 16:
            return 640, "Crystal"
        if other == 17:
            return 630, "Crystal"
        if other == 18:
            return 620, "Crystal"
        if other == 19:
            return 610, "Crystal"
        if other == 20:
            return 600, "Crystal"
        if 21 <= other <= 23:
            return 590, "Diamond ⬆"
        if 24 <= other <= 26:
            return 580, "Diamond ⬆"
        if 27 <= other <= 29:
            return 570, "Diamond ⬆"
        if 30 <= other <= 32:
            return 560, "Diamond ⬆"
        if 33 <= other <= 35:
            return 550, "Diamond ⬆"
        if 36 <= other <= 38:
            return 540, "Diamond"
        if 39 <= other <= 41:
            return 530, "Diamond"
        if 42 <= other <= 44:
            return 520, "Diamond"
        if 45 <= other <= 47:
            return 510, "Diamond"
        if 48 <= other <= 50:
            return 500, "Diamond"
        if 51 <= other <= 55:
            return 490, "Platinum ⬆"
        if 56 <= other <= 60:
            return 480, "Platinum ⬆"
        if 61 <= other <= 65:
            return 470, "Platinum ⬆"
        if 66 <= other <= 70:
            return 460, "Platinum ⬆"
        if 71 <= other <= 75:
            return 450, "Platinum ⬆"
        if 76 <= other <= 80:
            return 440, "Platinum"
        if 81 <= other <= 85:
            return 430, "Platinum"
        if 86 <= other <= 90:
            return 420, "Platinum"
        if 91 <= other <= 95:
            return 410, "Platinum"
        if 96 <= other <= 100:
            return 400, "Platinum"
        if 101 <= other <= 120:
            return 390, "Gold ⬆"
        if 121 <= other <= 140:
            return 380, "Gold ⬆"
        if 141 <= other <= 160:
            return 370, "Gold ⬆"
        if 161 <= other <= 180:
            return 360, "Gold ⬆"
        if 181 <= other <= 200:
            return 350, "Gold ⬆"
        if 201 <= other <= 220:
            return 340, "Gold"
        if 221 <= other <= 240:
            return 330, "Gold"
        if 241 <= other <= 260:
            return 320, "Gold"
        if 261 <= other <= 280:
            return 310, "Gold"
        if 281 <= other <= 300:
            return 300, "Gold"     
        if 301 <= other <= 330:
            return 290, "Silver ⬆"
        if 331 <= other <= 360:
            return 280, "Silver ⬆"
        if 361 <= other <= 390:
            return 270, "Silver ⬆"
        if 391 <= other <= 420:
            return 260, "Silver ⬆"
        if 421 <= other <= 450:
            return 250, "Silver ⬆"
        if 451 <= other <= 480:
            return 240, "Silver"
        if 481 <= other <= 510:
            return 230, "Silver"
        if 511 <= other <= 540:
            return 220, "Silver"
        if 541 <= other <= 570:
            return 210, "Silver"
        if 571 <= other <= 600:
            return 200, "Silver"
        if 601 <= other <= 640:
            return 190, "Bronze ⬆"     
        if 641 <= other <= 680:
            return 180, "Bronze ⬆"
        if 681 <= other <= 720:
            return 170, "Bronze ⬆"
        if 721 <= other <= 760:
            return 160, "Bronze ⬆"
        if 761 <= other <= 800:
            return 150, "Bronze ⬆"
        if 801 <= other <= 840:
            return 140, "Bronze"
        if 841 <= other <= 880:
            return 130, "Bronze"
        if 881 <= other <= 920:
            return 120, "Bronze"
        if 921 <= other <= 960:
            return 110, "Bronze"
        if 961 <= other <= 999:
            return 100, "Bronze"
        return 50, "Iron" # 1000 - infinite


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

    @app_commands.command(
        name="forfeit",
        description="Starts a forfeit vote to close the room by majority"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def forfeit(self, interaction: discord.Interaction):
        ch = interaction.channel

        # Solo funciona dentro de un hilo cuyo padre sea un #join permitido
        if not (isinstance(ch, Thread) and ch.parent_id in ALLOWED_JOIN_CHANNELS):
            return await interaction.response.send_message(
                "This command only works inside a room thread.",
                ephemeral=True
            )

        # Buscar la sala asociada a este hilo
        room = next((r for r in self.rooms.values() if r["thread"].id == ch.id), None)
        if not room:
            return await interaction.response.send_message(
                "⚠️ I can't find the room for this thread.",
                ephemeral=True
            )

        # Si la sala ya fue finalizada (submit o forfeit previo), no tiene sentido
        if room.get("finished", False):
            return await interaction.response.send_message(
                "This room is already finished.",
                ephemeral=True
            )

        # No permitir si hay otra votación (submit o forfeit) en curso
        if room.get("vote_in_progress", False):
            return await interaction.response.send_message(
                "There is already a vote in progress in this room.",
                ephemeral=True
            )

        # Jugadores actuales registrados en la sala (no bots)
        players = [m for m in room.get("players", []) if not m.bot]
        if not players:
            return await interaction.response.send_message(
                "There are no players in this room.",
                ephemeral=True
            )

        # Marcamos que hay una votación en curso para bloquear otros submits/forfeits
        room["vote_in_progress"] = True

        try:
            # Mensaje de poll (NO efímero, necesitamos reacciones)
            content = (
                f"Forfeit vote\n"
                f"React ✅ to forfeit and close the room, ❎ to cancel "
                f"(auto-close in {VOTE_TIMEOUT_FORFEIT}s)"
            )
            await interaction.response.send_message(content)
            poll_msg = await interaction.original_response()

            # Reacciones ✅ y ❎
            try:
                await poll_msg.add_reaction("✅")
                await poll_msg.add_reaction("❎")
            except Exception:
                pass

            # Esperamos el timeout completo y luego calculamos TODO al final,
            # para respetar a quienes se fueron del hilo durante la votación.
            await asyncio.sleep(float(VOTE_TIMEOUT_FORFEIT))

            # Refrescamos el mensaje para asegurar tener las reacciones finales
            try:
                poll_msg = await ch.fetch_message(poll_msg.id)
            except discord.NotFound:
                # Si el mensaje desapareció, cancelamos silenciosamente
                return

            thread: discord.Thread = ch  # para type hint

            # IDs de los jugadores que están registrados en la sala
            room_player_ids = {p.id for p in room.get("players", [])}

            # Jugadores ACTIVOS al cierre de la votación:
            # - Tienen que seguir en el hilo (thread.members)
            # - y deben estar en room["players"]
            active_players: set[int] = set()
            for tm in thread.members:
                # thread.members devuelve ThreadMember → obtenemos el User/Member real
                user = getattr(tm, "user", None) or tm
                if getattr(user, "bot", False):
                    continue
                if user.id in room_player_ids:
                    active_players.add(user.id)

            if not active_players:
                await ch.send(
                    "There are no active players left in this room. Forfeit vote canceled."
                )
                return

            yes_voters: set[int] = set()
            no_voters: set[int] = set()

            # Recorremos las reacciones y contamos votos SOLO de jugadores activos
            for reaction in poll_msg.reactions:
                emoji_str = str(reaction.emoji)
                if emoji_str not in ("✅", "❎"):
                    continue

                async for user in reaction.users():
                    if user.bot:
                        continue
                    if user.id not in active_players:
                        continue

                    if emoji_str == "✅":
                        yes_voters.add(user.id)
                    else:
                        no_voters.add(user.id)

            # Si alguien puso ambas reacciones, lo limpiamos (no cuenta ningún voto)
            conflicted = yes_voters & no_voters
            if conflicted:
                yes_voters -= conflicted
                no_voters -= conflicted

            total_active = len(active_players)
            yes_count = len(yes_voters)
            no_count = len(no_voters)

            # Mayoría estricta sobre los que QUEDAN en el hilo
            # (ej. 3 activos → se necesitan 2 ó más ✅)
            threshold = total_active // 2 + 1

            if yes_count >= threshold:
                # Forfeit aceptado por mayoría
                try:
                    await poll_msg.edit(
                        content=(
                            f"🛑 Forfeit accepted — closing room "
                            f"(✅ {yes_count} / ❎ {no_count}, {total_active} active players)"
                        )
                    )
                except Exception:
                    pass

                room["finished"] = True
                room["closed"] = True

                await ch.send(
                    "Room forfeited by majority of the players that remain in this thread. Closing room..."
                )

                # Notificar a otros cogs y cerrar el hilo
                rid = next(
                    (rid for rid, r in self.rooms.items() if r["thread"].id == ch.id),
                    None
                )
                if rid is not None:
                    self.bot.dispatch('room_finished', rid)

                # Cerramos el hilo con un pequeño delay
                asyncio.create_task(self.close_room(ch, delay=5, delete=True))

            else:
                # No se alcanzó la mayoría
                try:
                    await poll_msg.edit(
                        content=(
                            f"Forfeit vote did not pass "
                            f"(✅ {yes_count} / ❎ {no_count}, {total_active} active players; "
                            f"needed {threshold} ✅)"
                        )
                    )
                except Exception:
                    pass
                await ch.send("Forfeit vote failed. Room remains open.")

        finally:
            # Siempre liberar el flag de votación en curso
            room["vote_in_progress"] = False



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

        players_set = {m.id for m in players}
        ids_in_lines = []
        by_id: dict[int, tuple[str, list[int]]] = {}

        for idx, ln in enumerate(lines, start=1):
            mobj = ENTRY_RE.match(ln)
            if not mobj:
                return await ctx.send(f"Incorrect format for: {ln}")

            stats = list(map(int, mobj.group("stats").split(",")))
            if expected is not None:
                if sum(stats) != expected:
                    return await ctx.send(
                        f"there is an error on the calculation on line {idx}: {sum(stats)} ≠ {expected}"
                    )

            uid = int(mobj.group("id"))
            cc  = mobj.group("cc")
            ids_in_lines.append(uid)
            by_id[uid] = (cc, stats)

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

            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    reaction, user = await self.bot.wait_for("reaction_add", timeout=remaining, check=check)
                except asyncio.TimeoutError:
                    break

                if user.id in voted:
                    continue

                choice = str(reaction.emoji)
                voted.add(user.id)
                counts[choice] += 1

                if len(voted) == total_voters:
                    if counts["✅"] == total_voters:
                        ganador = "✅"
                    elif counts["❎"] == total_voters:
                        ganador = "❎"
                    break

            if ganador is None:
                if counts["✅"] > counts["❎"]:
                    ganador = "✅"
                elif counts["❎"] > counts["✅"]:
                    ganador = "❎"
                else:
                    ganador = "❎"

            try:
                await vote_msg.edit(
                    content=f"🗳️ Voting closed → {ganador}  (✅ {counts['✅']} / ❎ {counts['❎']})"
                )
            except Exception:
                pass

            if ganador != "✅":
                return await ctx.send("Submit declined. There might be an error, try doing it again.")

            # ==============================
            # VOTO APROBADO → aplicar MMR
            # ==============================
            medals  = {1: "🥇", 2: "🥈", 3: "🥉"}

            # ID global del result para la tabla results
            result_id = await self._next_result_id()

            # 1) Construir players_list UNA sola vez
            players_list = []
            for member in players:
                uid = member.id
                cc, stats = by_id[uid]
                old, current_role = await self.fetch_player(uid)

                # bandera guardada en DB, como fallback
                profile_cc = await self.db_pool.fetchval(
                    "SELECT country FROM players WHERE user_id=$1", uid
                ) or ""

                cc_up = (cc or "").upper()
                display_flag = country_flag(cc_up) or country_flag(profile_cc)

                total_points = sum(s * w for s, w in zip(stats, [4, 3, 2, 1, 0]))
                players_list.append(
                    {
                        "member": member,
                        "cc": cc,
                        "stats": stats,
                        "total": total_points,
                        "old": old,
                        "mmr_actual": old,  # ← base, se ajusta con Placement
                        "role": current_role,
                        "display_flag": display_flag,
                        "was_placement": (current_role == "Placement"),
                        "seed_mmr": None,
                        "seed_rank": None,
                    }
                )

            # Fase 1: aplicar seed si es Placement
            for p in players_list:
                if p["role"] == "Placement":
                    seed_mmr, seed_rank = self.placement_seed_from_stats(p["stats"])
                    p["mmr_actual"] = seed_mmr
                    p["seed_mmr"]   = seed_mmr
                    p["seed_rank"]  = seed_rank
                    p["was_placement"] = True
                else:
                    p["mmr_actual"] = p["old"]
                    p["seed_mmr"]   = None
                    p["seed_rank"]  = None

            # MMR promedio del room (ya con seeds de Placement)
            room_mmr = None
            if players_list:
                room_mmr = round(
                    sum(int(p["mmr_actual"]) for p in players_list) / len(players_list)
                )

            # Fase 2: Elo por pares
            players_list.sort(key=lambda x: x["total"], reverse=True)
            ratings = [int(p["mmr_actual"]) for p in players_list]
            order   = list(range(len(players_list)))
            deltas  = self.pairwise_deltas_simple(ratings, order, K=30, scale=400, cap=39)

            # Asegurar seasonplayers (por si acaso aún no existe)
            await self.db_pool.execute(
                """
                CREATE TABLE IF NOT EXISTS seasonplayers (
                    season_id   INTEGER NOT NULL,
                    user_id     BIGINT  NOT NULL,
                    mmr         INTEGER NOT NULL DEFAULT 0,
                    role        TEXT    NOT NULL DEFAULT 'Placement',
                    PRIMARY KEY (season_id, user_id)
                );
                """
            )

            season_row  = await self.get_current_season()
            season_id   = season_row["id"]
            season_name = season_row["name"]

            summary = []

            for idx, p in enumerate(players_list, start=1):
                mmr_prev  = int(p["mmr_actual"])
                mmr_delta = int(deltas[idx - 1])
                mmr_final = mmr_prev + mmr_delta
                role_name = get_rank_from_mmr(mmr_final)

                stats = p["stats"]
                s_p, s_g, s_g2, s_b, s_m = stats

                # UPSERT en seasonplayers
                await self.db_pool.execute(
                    """
                    INSERT INTO seasonplayers (season_id, user_id, mmr, role)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (season_id, user_id)
                    DO UPDATE SET mmr = EXCLUDED.mmr, role = EXCLUDED.role
                    """,
                    season_id,
                    p["member"].id,
                    mmr_final,
                    role_name,
                )

                # Insert único en match_history (con result_id + stats + pos)
                await self.db_pool.execute(
                    """
                    INSERT INTO match_history (
                        season_id,
                        result_id,
                        user_id,
                        pos,
                        s_perfect, s_great, s_good, s_bad, s_miss,
                        mmr_before, mmr_after, mmr_delta,
                        was_placement,
                        is_win
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    """,
                    season_id,
                    result_id,
                    p["member"].id,
                    idx,
                    s_p,
                    s_g,
                    s_g2,
                    s_b,
                    s_m,
                    mmr_prev,
                    mmr_final,
                    mmr_delta,
                    p["was_placement"],
                    mmr_delta >= 0,
                )

                # Actualizar roles de rango en Discord
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

                # Cambiar nick opcionalmente
                if ctx.guild.me.guild_permissions.manage_nicknames:
                    base = p["member"].display_name
                    nick = f"{base} [{role_name}]"
                    if len(nick) > 32:
                        nick = f"{base[:max(0, 32 - len(role_name) - 3)]} [{role_name}]"
                    try:
                        await p["member"].edit(nick=nick)
                    except (discord.Forbidden, discord.HTTPException) as e:
                        await ctx.send(f"⚠️ No pude cambiar nick de {p['member'].mention}: {e}")

                pos_str = medals.get(idx) or f"{idx}."
                name_with_flag = f"{p['member'].display_name} {p['display_flag']}".rstrip()
                summary.append(
                    (
                        pos_str,
                        name_with_flag,
                        p["total"],
                        p["stats"],
                        mmr_prev,
                        mmr_delta,
                        mmr_final,
                        p["was_placement"],
                    )
                )

            # Publicar resultados
            join_parent     = ctx.channel.parent
            result_chan_id  = JOIN_TO_RESULTS.get(join_parent.id) if join_parent else None
            result_chan     = await self._resolve_channel(result_chan_id, fallback=ctx.channel) if result_chan_id else ctx.channel

            gap_low  = room.get("gap_low")
            gap_high = room.get("gap_high")

            board_block = self._format_results_board(summary)

            gap_text = ""
            if gap_low is not None and gap_high is not None:
                gap_text = f" · Lv {gap_low}-{gap_high}"
                if room_mmr is not None:
                    gap_text += f" · Avg MMR {room_mmr}"

            header_line = f"ID: {result_id}{gap_text}"
            embed = discord.Embed(
                title=f"RESULTS — {season_name}",
                description=header_line + "\n" + board_block,
                color=0x00f7ff,
            )

            msg_result = await result_chan.send(embed=embed)
            await ctx.send(embed=embed)
            await result_chan.send("MMR Updated")
            await ctx.send("MMR Updated")

            # Guardar metadata del result
            await self.db_pool.execute(
                """
                INSERT INTO results (
                    result_id,
                    season_id,
                    season_name,
                    channel_id,
                    message_id,
                    gap_low,
                    gap_high,
                    room_mmr
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                result_id,
                season_id,
                season_name,
                msg_result.channel.id,
                msg_result.id,
                gap_low,
                gap_high,
                room_mmr,
            )

            room["closed"]   = True
            room["finished"] = True

            await ctx.send("This room has been submited results")

            rid = next((rid for rid, r in self.rooms.items() if r["thread"].id == ctx.channel.id), None)
            if rid is not None:
                self.bot.dispatch("room_finished", rid)

            asyncio.create_task(self.close_room(ctx.channel, delay=60, delete=True))

        finally:
            room["vote_in_progress"] = False



async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))
 #ed