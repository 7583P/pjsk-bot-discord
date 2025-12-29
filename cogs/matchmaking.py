import os, io, re, json, secrets
import random
import asyncio
import datetime
import aiohttp  # descarga catálogo canciones
import asyncpg  # PostgreSQL asíncrono
import discord
import logging
import datetime as dt
from discord import app_commands, Embed
from discord import ui, ButtonStyle
from discord import Thread, TextChannel, MessageType
from discord.ext import commands, tasks  # loops periódicos
from discord.ui import View, button
from zoneinfo import ZoneInfo

from helpers.image import generate_results_image
from PIL import Image


log = logging.getLogger(__name__)
# ———————————————————————————————————————————————
# =========================
# CONFIG (según tu spec #9)
# =========================
CATEGORY_IDS = {
    "en": 1371306302671687710,  # Categoría PJSK Global
    "jp": 1371951461612912802,  # Categoría PJSK JP
}

RESULTS_CHANNEL_IDS = {
    "en": 1371307931294892125,  # Results Global
    "jp": 1388515450534494389,  # Results JP
}

# ⚠️ REEMPLAZA con los IDs reales de los canales #join y #jp-join (NO categoría)
JOIN_CHANNEL_IDS = {
    "en": 1371307353437110282,  # <-- ID del canal #join
    "jp": 1378215330979254402,  # <-- ID del canal #jp-join
}

QUEUE_ROLE_IDS = {
    "en": 1451255910469996726,
    "jp": 1451255910469996726,
}

PING_ROLES_BY_JOIN_CHANNEL = {
    1371307353437110282: 1383874928968994928,  # #join -> @enTags
    1378215330979254402: 1428578933963489280,  # #jp-join -> @jpTags
}

ALLOWED_JOIN_CHANNELS = set(JOIN_CHANNEL_IDS.values())

JOIN_TO_RESULTS = {
    JOIN_CHANNEL_IDS["en"]: RESULTS_CHANNEL_IDS["en"],
    JOIN_CHANNEL_IDS["jp"]: RESULTS_CHANNEL_IDS["jp"],
}

JOIN_CHANNEL_CFG = {
    JOIN_CHANNEL_IDS["en"]: {
        "server": "en",
        "queue_role_id": QUEUE_ROLE_IDS["en"],
        "queue_role_name": "Rooms Queue",
    },
    JOIN_CHANNEL_IDS["jp"]: {
        "server": "jp",
        "queue_role_id": QUEUE_ROLE_IDS["jp"],
        "queue_role_name": "JP Rooms Queue",
    },
}


# Slots fijos de inicio
START_SLOTS_MINUTES = (0, 30)
START_WARN_WINDOW_MINUTES = 5  # máximo 5 minutos de avisos


def is_allowed_leave(ch: discord.abc.GuildChannel) -> bool:
    return getattr(ch, "id", None) in ALLOWED_JOIN_CHANNELS


# ———————————————————————————————————————————————

DB_PATH = "matchmaking.db"  # ignorado, pero por compatibilidad
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
JOIN_CHANNEL_NAME = "join"
DATABASE_URL = os.getenv("DB_HOST")  # postgresql://user:pass@host:port/dbname

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
    return t[: m.start(1)] + country_flag(m.group(1).upper())


# ————— Append: solo limita el rango de selección para canciones 'append' —————
APPEND_RANKS_ORDER = [
    "Legend",
    "Divine Star",
    "Star",
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
    "Diamond": (24, 26),
    "Diamond ⬆": (25, 27),
    "Crystal": (26, 28),
    "Crystal ⬆": (27, 29),
    "Master": (28, 30),
    "Master ⬆": (29, 31),
    "Champion": (30, 32),
    "Grand Champion": (31, 33),
    "Star": (32, 35),
    "Divine Star": (33, 37),
    "Legend": (34, 38),
}

# Tamaño del pool para non-append
NON_APPEND_POOL_LIMIT = 10

# Cuántas canciones append se meten al pool según el rango más alto presente
# Siempre intentamos meter hasta 5 canciones append en el pool
APPEND_POOL_LIMIT = 10

# Probabilidad (en %) de que la sala tenga appends (al menos 1) según el rango del room
APPEND_PROBABILITIES_PERCENT = {
    "Diamond": 1,  # 10% de salas con append
    "Diamond ⬆": 5,
    "Crystal": 10,
    "Crystal ⬆": 15,
    "Master": 20,
    "Master ⬆": 25,
    "Champion": 30,
    "Grand Champion": 35,
    "Star": 40,
    "Divine Star": 45,
    "Legend": 50,
}


def get_append_probability_for_rank(rank: str) -> float:
    """
    Devuelve la probabilidad de que la selección final tenga
    appends (al menos 1), como valor entre 0.0 y 1.0.
    """
    pct = APPEND_PROBABILITIES_PERCENT.get(rank, 0)
    return pct / 100.0


# Distribución de cuántos appends quiero CUANDO toca append.
# En este ejemplo:
# - Diamond: 70% de esas salas tendrán 1 append, 30% tendrán 2 appends.
APPEND_COUNTS_DISTRIB = {
    "Diamond": {
        1: 1,  # 10% de los rooms con append → 1 append
        2: 0.5,  # 5% → 2 appends
        3: 0.1,  # 1% de los rooms con append → 1 append
        4: 0.05,  # 0.5% → 2 appends
        5: 0.01,  # 0.01% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Diamond ⬆": {
        1: 1,  # 15% de los rooms con append → 1 append
        2: 0.5,  # 7.5% → 2 appends
        3: 0.1,  # 1.15% de los rooms con append → 1 append
        4: 0.05,  # 0.15% → 2 appends
        5: 0.01,  # 0.015% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Crystal": {
        1: 1,  # 20% de los rooms con append → 1 append
        2: 0.5,  # 10% → 2 appends
        3: 0.1,  # 2% de los rooms con append → 1 append
        4: 0.05,  # 0.20% → 2 appends
        5: 0.01,  # 0.020% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Crystal ⬆": {
        1: 1,  # 25% de los rooms con append → 1 append
        2: 0.5,  # 12.5% → 2 appends
        3: 0.1,  # 2.5% de los rooms con append → 1 append
        4: 0.05,  # 1.25% → 2 appends
        5: 0.01,  # 0.25% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Master": {
        1: 1,  # 30% de los rooms con append → 1 append
        2: 0.5,  # 15% → 2 appends
        3: 0.1,  # 3% de los rooms con append → 1 append
        4: 0.05,  # 1.5% → 2 appends
        5: 0.01,  # 0.030% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Master ⬆": {
        1: 1,  # 35% de los rooms con append → 1 append
        2: 0.5,  # 17.5% → 2 appends
        3: 0.1,  # 3.5% de los rooms con append → 1 append
        4: 0.05,  # 1.75% → 2 appends
        5: 0.01,  # 0.35% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Champion": {
        1: 1,  # 40% de los rooms con append → 1 append
        2: 0.5,  # 20% → 2 appends
        3: 0.1,  # 4% de los rooms con append → 1 append
        4: 0.05,  # 2% → 2 appends
        5: 0.01,  # 0.40% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Grand Champion": {
        1: 1,  # 40% de los rooms con append → 1 append
        2: 0.5,  # 20% → 2 appends
        3: 0.1,  # 4% de los rooms con append → 1 append
        4: 0.05,  # 2% → 2 appends
        5: 0.01,  # 0.40% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Star": {
        1: 1,  # 45% de los rooms con append → 1 append
        2: 0.5,  # 22.5% → 2 appends
        3: 0.1,  # 4.5% de los rooms con append → 1 append
        4: 0.05,  # 2.25% → 2 appends
        5: 0.01,  # 0.45% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Divine Star": {
        1: 1,  # 45% de los rooms con append → 1 append
        2: 0.5,  # 22.5% → 2 appends
        3: 0.1,  # 4.5% de los rooms con append → 1 append
        4: 0.05,  # 2.25% → 2 appends
        5: 0.01,  # 0.45% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
    "Legend": {
        1: 1,  # 50% de los rooms con append → 1 append
        2: 0.5,  # 25% → 2 appends
        3: 0.1,  # 5% de los rooms con append → 1 append
        4: 0.05,  # 2.50% → 2 appends
        5: 0.01,  # 0.50% de los rooms con append → 1 append
    },
    # puedes ir definiendo otros rangos aquí si quieres
}


def pick_append_count_for_rank(rank: str, max_available: int) -> int:
    """
    El distrib se interpreta como PESOS (no como probabilidades que suman 1).
    """
    if max_available <= 0:
        return 0

    distrib = APPEND_COUNTS_DISTRIB.get(rank)
    if not distrib:
        return 1  # fallback: 1 append si hay

    items = sorted(distrib.items())  # (k, weight)
    total_w = sum(float(w) for _, w in items if float(w) > 0)
    if total_w <= 0:
        return 1

    r = random.random() * total_w
    acc = 0.0
    for k, w in items:
        w = float(w)
        if w <= 0:
            continue
        acc += w
        if r <= acc:
            return min(int(k), max_available)

    return min(int(items[-1][0]), max_available)


# Prioridad global de dificultades, ahora incluyendo hard
DIFFICULTY_PRIORITY = ("normal", "hard", "expert", "master", "append")


# Mapeo de rangos a dificultades permitidas
ALLOWED_DIFFS_BY_RANK = {
    "Placement": ("expert",),
    "Iron": ("normal", "hard"),
    "Iron ⬆": ("normal", "hard"),
    "Bronze": ("normal", "hard"),
    "Bronze ⬆": ("hard", "expert"),
    "Silver": ("hard", "expert"),
    "Silver ⬆": ("hard", "expert"),
    "Gold": ("hard", "expert"),
    "Gold ⬆": ("hard", "expert", "master"),
    "Platinum": ("hard", "expert", "master"),
    "Platinum ⬆": ("hard", "expert", "master"),
    "Diamond": ("hard", "expert", "master", "append"),
    "Diamond ⬆": ("hard", "expert", "master", "append"),
    "Crystal": ("expert", "master", "append"),
    "Crystal ⬆": ("expert", "master", "append"),
    "Master": ("expert", "master", "append"),
    "Master ⬆": ("expert", "master", "append"),
    "Champion": ("expert", "master", "append"),
    "Grand Champion": ("expert", "master", "append"),
    "Star": ("expert", "master", "append"),
    "Divine Star": ("master", "append"),
    "Legend": ("master", "append"),
}

# Nuevos rangos con gap exacto
BRACKET_RANGES = {
    "Placement": (24, 28),
    "Iron": (14, 18),
    "Iron ⬆": (15, 19),
    "Bronze": (16, 20),
    "Bronze ⬆": (17, 21),
    "Silver": (18, 22),
    "Silver ⬆": (19, 23),
    "Gold": (20, 24),
    "Gold ⬆": (21, 25),
    "Platinum": (22, 26),
    "Platinum ⬆": (23, 27),
    "Diamond": (24, 28),
    "Diamond ⬆": (25, 29),
    "Crystal": (26, 30),
    "Crystal ⬆": (27, 31),
    "Master": (28, 32),
    "Master ⬆": (29, 33),
    "Champion": (30, 34),
    "Grand Champion": (31, 35),
    "Star": (32, 36),
    "Divine Star": (33, 37),
    "Legend": (34, 38),
}

MIN_LEVEL_GAP = 3
ROOM_CAPACITY = 5
VOTE_TIMEOUT_SUBMIT = 30
VOTE_TIMEOUT_FORFEIT = 30
LIMA_TZ = ZoneInfo("America/Lima")

PHASE_FUTURE = "FUTURE"  # en cola (sin hilo)
PHASE_PREPARED = "PREPARED"  # hilo creado + canciones posteadas (locked-in)
PHASE_STARTED = "STARTED"  # inicio real en :00/:30
PHASE_PENDING = "PENDING"  # unfilled rooms past queue time
PHASE_FINISHED = "FINISHED"


def dynamic_range(counts: dict[str, int]) -> tuple[int, int]:
    centers, gaps = [], []
    for rank, num in counts.items():
        if num > 0:
            lo, hi = BRACKET_RANGES[rank]
            center = round((lo + hi) / 2)
            gap = hi - lo
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
    avg_gap = max(MIN_LEVEL_GAP, round(sum(gaps) / len(gaps)))

    low = max(1, avg_center - avg_gap // 2)
    high = low + avg_gap
    return low, high


def get_rank_from_mmr(mmr: int) -> str:

    if mmr <= 49:
        return "Iron"
    elif mmr <= 99:
        return "Iron ⬆"
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
    elif mmr <= 849:
        return "Champion"
    elif mmr <= 899:
        return "Grand Champion"
    elif mmr <= 949:
        return "Star"
    elif mmr <= 999:
        return "Divine Star"
    else:
        return "Legend"


RANK_ROLE_IDS = {
    "Placement": 1371321594068336811,
    "Iron": 1394444407536881845,
    "Iron ⬆": 1446954780915273778,
    "Bronze": 1371324225838645339,
    "Bronze ⬆": 1433827677713858732,
    "Silver": 1389343997100560514,
    "Silver ⬆": 1433827565222494383,
    "Gold": 1371324328708149328,
    "Gold ⬆": 1433827466744561720,
    "Platinum": 1389343805521789098,
    "Platinum ⬆": 1434564540619554960,
    "Diamond": 1371324561542484108,
    "Diamond ⬆": 1440477553151184916,
    "Crystal": 1394445724703527012,
    "Crystal ⬆": 1440477788501966858,
    "Master": 1394635883466199110,
    "Master ⬆": 1440478019012530267,
    "Champion": 1371323543501144115,
    "Grand Champion": 1449495589153935421,
    "Star": 1394444744892874832,
    "Divine Star": 1449495651535552729,
    "Legend": 1371323380510363749,
}

# ——————————————————————————————
# Constantes de Placement
# ——————————————————————————————
PLACEMENT_ROLE_ID = 1371321594068336811


class Top100View(View):
    def __init__(
        self,
        db_pool: asyncpg.Pool,
        interaction: discord.Interaction,
        season_id: int,
        season_name: str,
    ):
        super().__init__(timeout=None)
        self.db = db_pool
        self.inter = interaction
        self.season_id = season_id
        self.season_name = season_name
        self.page = 0
        self.per_page = 20
        self._join_lock = asyncio.Lock()

    async def build_embed(self) -> Embed:
        # total jugadores en ESA season
        total_all = await self.db.fetchval(
            "SELECT COUNT(*) FROM seasonplayers WHERE season_id = $1", self.season_id
        )
        total = min(total_all or 0, 100)
        max_page = (total - 1) // self.per_page if total else 0

        offset = self.page * self.per_page
        rows = await self.db.fetch(
            """
            SELECT p.user_id, sp.mmr, sp.role
            FROM players p
            INNER JOIN seasonplayers sp ON p.user_id = sp.user_id
            WHERE sp.season_id = $3
            ORDER BY sp.mmr DESC
            OFFSET $1 LIMIT $2
            """,
            offset,
            self.per_page,
            self.season_id,
        )

        embed = Embed(title=f"PJSK LOUNGE TOP 100 — {self.season_name}", color=0x00FFFB)
        header = "TOP| Name                     |   MMR   | Rank\n"
        separator = "---+--------------------------+---------+-----------\n"
        lines = []
        for idx, r in enumerate(rows, start=offset + 1):
            member = self.inter.guild.get_member(r["user_id"])
            name = (member.display_name if member else f"ID {r['user_id']}")[:25]
            mmr = f"{r['mmr']:,}"
            rank = r["role"]
            lines.append(f"{idx:>2} | {name:<25} | {mmr:>7} | {rank}")

        embed.description = (
            "```\n"
            + header
            + separator
            + ("\n".join(lines) if lines else "(no players)\n")
            + f"\n```Página {self.page+1}/{max_page+1}"
        )
        return embed

    @button(emoji="◀️", style=ButtonStyle.secondary)
    async def prev_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.page > 0:
            self.page -= 1

        total_all = await self.db.fetchval(
            "SELECT COUNT(*) FROM seasonplayers WHERE season_id = $1", self.season_id
        )
        total = min(total_all or 0, 100)
        max_page = (total - 1) // self.per_page if total else 0

        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page == max_page

        new_embed = await self.build_embed()
        await interaction.response.edit_message(embed=new_embed, view=self)

    @button(emoji="▶️", style=ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        total_all = await self.db.fetchval(
            "SELECT COUNT(*) FROM seasonplayers WHERE season_id = $1", self.season_id
        )
        total = min(total_all or 0, 100)
        max_page = (total - 1) // self.per_page if total else 0

        if self.page < max_page:
            self.page += 1

        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page == max_page

        new_embed = await self.build_embed()
        await interaction.response.edit_message(embed=new_embed, view=self)


class Matchmaking(commands.Cog):
    RAW_EN = (
        "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-en-diff/main"
    )
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
    def pairwise_deltas_simple(
        ratings: list[int],
        order: list[int],
        K: int = 50,
        scale: int = 400,
        cap: int | None = 39,
    ) -> list[int]:
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

    @staticmethod
    def _apply_tie_logic_placement_priority(
        players: list[dict], deltas: list[int]
    ) -> list[int]:
        """
        players: lista de dicts con al menos:
            - 'total'          -> puntos PGGBM de la partida
            - 'was_placement'  -> bool indicando si estaba en Placement en ESTE match
        deltas: lista de ΔMMR calculados por pairwise_deltas_simple, alineada por índice.

        Regla:
        - Se agrupa por 'total' (puntos), asumiendo que players ya viene ordenado por posición.
        - Si el grupo tiene solo Placement o solo no-Placement:
              todos reciben el mejor Δ del grupo.
        - Si el grupo tiene mezcla Placement + no-Placement:
              • todos los Placement del grupo reciben el mejor Δ de TODO el grupo.
              • todos los no-Placement del grupo reciben el mejor Δ entre los no-Placement.
        """
        if len(players) <= 1:
            return deltas

        adjusted = deltas[:]

        i = 0
        while i < len(players):
            j = i + 1
            # grupo de empate por 'total'
            while j < len(players) and players[j]["total"] == players[i]["total"]:
                j += 1

            group_size = j - i
            if group_size > 1:
                group_indices = list(range(i, j))
                placement_idx = [
                    k for k in group_indices if players[k].get("was_placement", False)
                ]
                nonplacement_idx = [
                    k
                    for k in group_indices
                    if not players[k].get("was_placement", False)
                ]

                if placement_idx and nonplacement_idx:
                    # Grupo mixto: Placement + no-Placement
                    group_best = max(deltas[k] for k in group_indices)
                    best_non = max(deltas[k] for k in nonplacement_idx)

                    # Placement recibe SIEMPRE el mejor Δ del grupo
                    for k in placement_idx:
                        adjusted[k] = group_best

                    # No-Placement reciben el mejor Δ entre ellos
                    for k in nonplacement_idx:
                        adjusted[k] = best_non
                else:
                    # Grupo homogéneo (solo Placement o solo no-Placement)
                    best_all = max(deltas[k] for k in group_indices)
                    for k in group_indices:
                        adjusted[k] = best_all

            i = j

        return adjusted

    def _is_placement_member(self, member: discord.Member) -> bool:
        # Placement se determina por rol del servidor (no por MMR)
        return any(r.id == PLACEMENT_ROLE_ID for r in getattr(member, "roles", []))

    def _minute_key_utc(self) -> int:
        now = datetime.datetime.utcnow().replace(second=0, microsecond=0)
        return int(now.timestamp())

    def _slot_key_utc(self) -> tuple[int, int, int, int]:
        now = datetime.datetime.utcnow()
        return (now.year, now.month, now.day, now.hour * 60 + now.minute)

    def _next_half_hour_slot_utc(self, now: datetime.datetime) -> datetime.datetime:
        # próximo :00 o :30 (UTC)
        base = now.replace(second=0, microsecond=0)
        m = base.minute
        if m < 30:
            return base.replace(minute=30)
        # siguiente hora :00
        nxt = base + datetime.timedelta(hours=1)
        return nxt.replace(minute=0)

    def _now_lima(self) -> dt.datetime:
        return dt.datetime.now(tz=LIMA_TZ)

    # Nuevo schedule:
    # Nuevo schedule (Lima):
    # PREP:  :25–:29 y :55–:59
    # START: :30–:34 y :00–:04
    PREP_WINDOWS = ((55, 59), (25, 29))  # inclusive
    START_WINDOWS = ((0, 4), (30, 34))  # inclusive

    def _slot_start_from_prep_start(
        self, now: dt.datetime, prep_start: int
    ) -> dt.datetime:
        """
        PREP start = 55//25  -> slot start = 00/30.
        Retorna datetime local Lima del start real del slot.
        """
        base = now.replace(second=0, microsecond=0)
        slot_minute = (prep_start + 5) % 60
        slot_dt = base.replace(minute=slot_minute)

        # Si wrappea (55->00), avanzamos hora
        if slot_minute < prep_start:
            slot_dt += dt.timedelta(hours=1)

        return slot_dt

    def _next_occurrence_of_minute(self, now: dt.datetime, minute: int) -> dt.datetime:
        """
        Próxima ocurrencia (>= now) de un minuto específico (:25/:55) en hora local.
        Si ya pasó en esta hora, usa la siguiente hora.
        """
        base = now.replace(second=0, microsecond=0)
        cand = base.replace(minute=minute)
        if cand <= base:
            cand += dt.timedelta(hours=1)
        return cand

    def _find_room_by_thread_id(self, thread_id: int) -> tuple[int | None, dict | None]:
        """
        Devuelve (rid, info) para un thread activo (no finished) por su thread_id.
        """
        for rid, info in self.rooms.items():
            if info.get("finished", False):
                continue
            if int(info.get("thread", 0) or 0) == int(thread_id):
                return rid, info
        return None, None


    async def _announce_queue_countdown_prep_minute(
        self, guild: discord.Guild, join_channel: discord.TextChannel, now: dt.datetime
    ):
        prep_start = self._in_prep_window(now)  # 55 / 25
        if prep_start is None:
            return

        slot_dt = self._slot_start_from_prep_start(now, prep_start)

        # now puede venir con segundos; lo normalizamos para que 14:59:xx -> 1 min left
        now0 = now.replace(second=0, microsecond=0)
        mins_left = int((slot_dt - now0).total_seconds() // 60)

        # Solo 5..1
        if mins_left < 1 or mins_left > 5:
            return

        join_id = join_channel.id
        ordered = await self._get_ordered_queue_members(guild, join_id)
        qlen = len(ordered)

        # Need incluso si no hay nadie
        if qlen == 0:
            need = ROOM_CAPACITY
        else:
            rem = qlen % ROOM_CAPACITY
            need = 0 if rem == 0 else (ROOM_CAPACITY - rem)

        slot_ts = int(slot_dt.timestamp())

        # Ping seguro (sin KeyError) — solo si:
        # - hay gente en cola (qlen > 0)
        # - el siguiente room NO está lleno todavía (need > 0)
        # - es el aviso de 5 minutos
        ping_role_id = PING_ROLES_BY_JOIN_CHANNEL.get(join_id)
        do_ping = (
            ping_role_id is not None
            and mins_left == 5
            and qlen > 0
            and need > 0
        )
        ping_part = f"<@&{ping_role_id}> " if do_ping else ""

        # Texto cuando ya está completo
        if need == 0:
            need_txt = "Next room is already full."
        else:
            need_txt = f"Need ({need}) player(s) to fill the next room."

        try:
            await join_channel.send(
                f"**{mins_left} min left** — Queue starting at <t:{slot_ts}:T> (<t:{slot_ts}:R>). "
                f"{ping_part}{need_txt} "
                f"Type `/c` to join the queue or `/d` to drop the queue."
            )
        except Exception:
            pass


    async def _pend_unfilled_queue_on_slot(
        self, guild: discord.Guild, join_channel: discord.TextChannel, now: dt.datetime
    ):
        """
        En el minuto exacto del slot (:00 / :30):
        Si la queue no completa el siguiente grupo de 5, NO crea thread "unfilled".
        Solo anuncia y deja a esos jugadores en la queue.
        """
        join_id = join_channel.id
        ordered = await self._get_ordered_queue_members(guild, join_id)
        if not ordered:
            return

        rem = len(ordered) % ROOM_CAPACITY
        if rem == 0:
            return  # no hay grupo incompleto

        leftovers = ordered[-rem:]
        need = ROOM_CAPACITY - rem

        mentions = " ".join(m.mention for m in leftovers)
        try:
            await join_channel.send(
                f"⛔ Slot reached and the next room did not fill. "
                f"Waiting for **{need}** player(s) to complete the room. {mentions}\n"
                f"Type `/c` to join the queue or `/d` to drop the queue."
            )
        except Exception:
            pass

        # ✅ IMPORTANTE:
        # - NO crear thread
        # - NO mover jugadores fuera de self.queues
        # - NO tocar queue_join_order
        # Se quedan esperando para el siguiente PREP/slot.

    async def _purge_unfilled_queue_on_slot(
        self, guild: discord.Guild, join_channel: discord.TextChannel, now: dt.datetime
    ):
        """
        En el minuto exacto del slot (:00 / :20 / :40):
        si la cola NO completó el siguiente grupo de 5, elimina a esos jugadores de la queue.
        """
        join_id = join_channel.id
        ordered = await self._get_ordered_queue_members(guild, join_id)
        if not ordered:
            return

        rem = len(ordered) % ROOM_CAPACITY
        if rem == 0:
            return  # no hay grupo incompleto

        leftovers = ordered[-rem:]  # los que quedaron “en la sala que no se llenó”
        for m in leftovers:
            self.queues.get(join_id, set()).discard(m.id)
            self.queue_join_order.get(join_id, {}).pop(m.id, None)

        # remover roles de queue
        try:
            await self._remove_queue_roles(leftovers)
        except Exception:
            pass

        # mensaje público con quiénes salieron
        mentions = " ".join(m.mention for m in leftovers)
        try:
            await join_channel.send(
                f"Not enough players to start the room, Dropping **{len(leftovers)}** player(s) from queue: {mentions}\n"
                f"Type `/c` to re-join for the next slot."
            )
        except Exception:
            pass

        # refrescar #rooms
        try:
            self.bot.dispatch("room_updated", None)
        except Exception:
            pass

    async def _announce_next_queue_windows_on_00(self, join_channel, now):
        # Próximos PREP windows típicos: :25, :55
        candidates = [
            (25, self._next_occurrence_of_minute(now, 25)),
            (55, self._next_occurrence_of_minute(now, 55)),
        ]
        candidates.sort(key=lambda x: x[1])

        lines = ["Next queue will start at:"]
        for minute, t_prep in candidates:
            ts_prep = int(t_prep.timestamp())
            t_start = t_prep + dt.timedelta(minutes=5)
            ts_start = int(t_start.timestamp())

            lines.append(
                f"• **:{minute:02d}** → <t:{ts_prep}:R>  | rooms starts at <t:{ts_start}:T> (<t:{ts_start}:R>)"
            )

        lines.append("Type `/c` to join the queue or `/d` to drop the queue.")

        try:
            await join_channel.send("\n".join(lines))
        except Exception:
            pass

    def _window_start_minute(
        self, minute: int, windows: tuple[tuple[int, int], ...]
    ) -> int | None:
        for a, b in windows:
            if a <= minute <= b:
                return a
        return None

    def _in_prep_window(self, now: dt.datetime) -> int | None:
        return self._window_start_minute(now.minute, self.PREP_WINDOWS)

    def _in_start_window(self, now: dt.datetime) -> int | None:
        return self._window_start_minute(now.minute, self.START_WINDOWS)

    def _slot_key(self, dt: datetime) -> tuple[int, int, int, int]:
        # clave para evitar doble ejecución en la misma marca
        return (dt.year, dt.month, dt.day, dt.hour * 60 + dt.minute)

    def _rank_label_from_mmr(self, mmr: int) -> str:
        # Unifica el label del room con el mapping global
        return get_rank_from_mmr(int(mmr))

    def _build_thread_name(
        self, idx: int, dt_local: datetime, avg_mmr: int, rank: str
    ) -> str:
        day = dt_local.strftime("%Y-%m-%d")
        hhmm = dt_local.strftime("%H%M")
        return f"r{idx} {day} {hhmm} a{avg_mmr} {rank}"

    async def _get_ordered_queue_members(
        self, guild: discord.Guild, join_channel_id: int
    ) -> list[discord.Member]:
        ids = list(self.queues.get(join_channel_id, set()))
        members: list[discord.Member] = []
        for uid in ids:
            m = guild.get_member(uid)
            if m:
                members.append(m)

        placements = [m for m in members if self._is_placement_member(m)]
        nonplacements = [m for m in members if not self._is_placement_member(m)]

        mmr_map = await self._fetch_mmrs_batch([m.id for m in nonplacements])
        join_order = self.queue_join_order.get(join_channel_id, {})

        nonplacements.sort(
            key=lambda m: (
                -int(mmr_map.get(m.id, 0)),
                join_order.get(m.id, 10**18),  # 👈 desempate por orden real de join
            )
        )

        placements.sort(key=lambda m: join_order.get(m.id, 10**18))

        return nonplacements + placements

    async def _prepare_rooms_for_join_channel(
        self, guild: discord.Guild, join_channel: discord.TextChannel
    ):
        """
        PREP (:55/:25 windows) -> crea threads y mueve jugadores,
        PERO NO postea canciones. Las canciones se postean en el slot (:00/:20/:40).
        """
        join_id = join_channel.id
        ordered = await self._get_ordered_queue_members(guild, join_id)

        # grupos completos
        groups = [
            ordered[i : i + ROOM_CAPACITY]
            for i in range(0, len(ordered), ROOM_CAPACITY)
        ]
        full_groups = [g for g in groups if len(g) == ROOM_CAPACITY]
        if not full_groups:
            return

        now = self._now_lima()

        # slot real al que corresponde este PREP (55->00, 15->20, 35->40)
        prep_start = self._in_prep_window(now)  # 55/15/35
        if prep_start is None:
            return
        slot_dt = self._slot_start_from_prep_start(now, prep_start)
        slot_ts = int(slot_dt.timestamp())

        server_code = JOIN_CHANNEL_CFG.get(join_id, {}).get("server", "en")

        for i, group in enumerate(full_groups, start=1):
            # calcular avg mmr para naming (en base al grupo)
            mmr_map = await self._fetch_mmrs_batch([m.id for m in group])
            mmr_vals = [int(mmr_map.get(m.id, 0)) for m in group]
            avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0
            rank = self._rank_label_from_mmr(avg)

            # IMPORTANTE: usar slot_dt (no now) para nombre del thread
            thread_name = self._build_thread_name(i, slot_dt, avg, rank)

            rid, info = await self._create_room(
                join_channel, server_code=server_code, thread_name=thread_name
            )

            # mover de cola a room
            for m in group:
                self.queues[join_id].discard(m.id)
                await self._add_member_to_room(m, rid, info)
                self.queue_join_order.get(join_id, {}).pop(m.id, None)

            # marcar como FUTURE (espera al slot para postear songs)
            info["phase"] = PHASE_FUTURE
            info["locked_in_thread"] = True
            info["slot_start_ts"] = slot_ts
            info["avg_mmr"] = avg
            info["rank_label"] = rank
            info["started"] = False
            info["songs_posted"] = False

            mentions = " ".join(m.mention for m in group)

            # Ping + aviso de cuándo se publican songs
            allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
            await self.bot.get_channel(info["join_channel_id"]).get_thread(
                info["thread"]
            ).send(
                f"{mentions}\n"
                f"✅ You are locked in for this slot.\n"
                f"🎵 Songs will be displayed at <t:{slot_ts}:T> (<t:{slot_ts}:R>).",
                allowed_mentions=allowed,
            )

            # quitar rol cola
            try:
                await self._remove_queue_roles(group)
            except Exception:
                pass

            # notificar a Rooms cog
            self.bot.dispatch("room_updated", rid)

    async def _start_pending_room(self, rid: int):
        info = self.rooms.get(rid, None)
        if not info:
            return

        if info.get("finished", False):
            return

        if info.get("phase") != PHASE_PENDING:
            return
        
        if info.get("slot_start_ts") is not None:
            return


        players = [m for m in info.get("players", []) if m and not m.bot]
        n = len(players)

        # si se quedaron sin gente
        if n < 2:
            info["finished"] = True
            info["closed"] = True
            try:
                await self._remove_queue_roles(players)
            except Exception:
                pass
            try:
                await self.bot.get_channel(info["join_channel_id"]).get_thread(
                    info["thread"]
                ).send("⛔ Room did not start (not enough players). Closing room.")
            except Exception:
                pass
            try:
                self.bot.dispatch("room_finished", rid)
            except Exception:
                pass
            asyncio.create_task(
                self.close_room(
                    self.bot.get_channel(info["join_channel_id"]).get_thread(
                        info["thread"]
                    ),
                    delay=5,
                    delete=True,
                )
            )
            return

        # 1) Postear songs EXACTAMENTE en el slot
        try:
            await self.launch_song_poll(
                info
            )  # esto postea "Selected songs..." en el thread
        except Exception:
            pass

        # 2) Marcar started
        info["phase"] = PHASE_STARTED
        info["started"] = True
        info["started_at"] = int(self._now_lima().timestamp())

        try:
            await self.bot.get_channel(info["join_channel_id"]).get_thread(
                info["thread"]
            ).send("✅ Match started (unfilled room). Good luck.")
        except Exception:
            pass

        self.bot.dispatch("room_updated", rid)

    async def _start_future_rooms_for_slot(self, join_channel_id: int, slot_ts: int):
        """
        En el minuto exacto del slot (:00/:30):
        - Para rooms FUTURE cuyo slot_start_ts == slot_ts:
            * postea canciones (launch_song_poll)
            * marca STARTED
        """
        started_ts = slot_ts

        for rid, info in list(self.rooms.items()):
            if info.get("join_channel_id") != join_channel_id:
                continue
            if info.get("finished", False):
                continue

            if info.get("phase") != PHASE_FUTURE:
                continue

            if int(info.get("slot_start_ts", 0)) != int(slot_ts):
                continue

            players = [m for m in info.get("players", []) if m and not m.bot]
            n = len(players)

            # si se quedaron sin gente
            if n < 2:
                info["finished"] = True
                info["closed"] = True
                try:
                    await self._remove_queue_roles(players)
                except Exception:
                    pass
                try:
                    await self.bot.get_channel(info["join_channel_id"]).get_thread(
                        info["thread"]
                    ).send("⛔ Room did not start (not enough players). Closing room.")
                except Exception:
                    pass
                try:
                    self.bot.dispatch("room_finished", rid)
                except Exception:
                    pass
                asyncio.create_task(
                    self.close_room(
                        self.bot.get_channel(info["join_channel_id"]).get_thread(
                            info["thread"]
                        ),
                        delay=5,
                        delete=True,
                    )
                )
                continue

            # 1) Postear songs EXACTAMENTE en el slot
            try:
                await self.launch_song_poll(
                    info
                )  # esto postea "Selected songs..." en el thread
            except Exception:
                pass

            # 2) Marcar started
            info["phase"] = PHASE_STARTED
            info["started"] = True
            info["started_at"] = int(started_ts)  # <-- clave que usa el board
            # info["started_ts"] = started_ts      # opcional si quieres mantenerlo, pero no es usado por el board

            try:
                await self.bot.get_channel(info["join_channel_id"]).get_thread(
                    info["thread"]
                ).send(" Match started, Good luck and have fun!")
            except Exception:
                pass

            self.bot.dispatch("room_updated", rid)

    async def _fetch_mmrs_batch(self, user_ids: list[int]) -> dict[int, int]:
        """
        Lee mmr actual de seasonplayers (season activa) en batch.
        Missing -> 0.
        """
        if not user_ids:
            return {}
        season = await self.get_current_season()
        if not season:
            return {uid: 0 for uid in user_ids}
        season_id = int(season["id"])

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, mmr
                FROM seasonplayers
                WHERE season_id = $1
                AND user_id = ANY($2::bigint[])
                """,
                season_id,
                user_ids,
            )
        out = {int(r["user_id"]): int(r["mmr"]) for r in rows}
        for uid in user_ids:
            out.setdefault(uid, 0)
        return out

    async def _assign_queue_role_for_join(
        self, member: discord.Member, join_channel_id: int
    ):
        cfg = JOIN_CHANNEL_CFG.get(join_channel_id)
        if not cfg:
            return
        role_id = cfg.get("queue_role_id")
        if not role_id:
            return
        role = member.guild.get_role(int(role_id))
        if not role:
            return
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Joined matchmaking queue")
            except Exception:
                pass

    async def _remove_queue_roles(self, members: list[discord.Member]):
        """
        Quita ambos roles de queue (EN/JP) por seguridad.
        """
        role_ids = {
            int(v["queue_role_id"])
            for v in JOIN_CHANNEL_CFG.values()
            if v.get("queue_role_id")
        }
        if not role_ids:
            return
        for m in members:
            to_remove = [r for r in m.roles if r.id in role_ids]
            if to_remove:
                try:
                    await m.remove_roles(
                        *to_remove, reason="Left queue / finished match"
                    )
                except Exception:
                    pass

    async def _rebalance_parent_queue(
        self, guild: discord.Guild, join_channel: discord.TextChannel
    ):
        """
        Regla:
        - Máximo 5 por sala.
        - Orden global por MMR desc.
        - Placement SIEMPRE al final (sala más baja).
        - Crea/elimina threads según necesidad (uno por sala).
        - Room-1 siempre contiene los 5 MMR más altos.
        """
        parent_id = join_channel.id

        # Solo salas NO iniciadas
        active_rooms = [
            (rid, info)
            for rid, info in self._rooms_for_parent(parent_id)
            if not info.get("started", False) and not info.get("finished", False)
        ]

        # Unión de jugadores (sin duplicados)
        seen: set[int] = set()
        members_all: list[discord.Member] = []
        for rid, info in active_rooms:
            for m in info.get("players", []):
                if m and m.id not in seen:
                    seen.add(m.id)
                    members_all.append(m)

        if not members_all:
            # Si no hay nadie en cola, limpia rooms vacíos no-started
            for rid, info in active_rooms:
                if not info.get("players"):
                    try:
                        await self.bot.get_channel(info["join_channel_id"]).get_thread(
                            info["thread"]
                        ).delete(reason="Empty queue room")
                    except Exception:
                        pass
                    self.rooms.pop(rid, None)
            return

        # Clasificar placement vs no-placement
        placements = [m for m in members_all if self._is_placement_member(m)]
        nonplacements = [m for m in members_all if not self._is_placement_member(m)]

        mmr_map = await self._fetch_mmrs_batch([m.id for m in nonplacements])

        nonplacements.sort(key=lambda m: (-int(mmr_map.get(m.id, 0)), m.id))
        # Placement al final, estable por id
        placements.sort(key=lambda m: m.id)

        ordered = nonplacements + placements

        # Agrupar en chunks de 5
        groups: list[list[discord.Member]] = [
            ordered[i : i + ROOM_CAPACITY]
            for i in range(0, len(ordered), ROOM_CAPACITY)
        ]

        # Asegurar cantidad de rooms = len(groups)
        # Reusar rooms existentes (ordenadas por created_ts), crear nuevas si faltan
        active_rooms.sort(key=lambda t: (int(t[1].get("created_ts", 0)), t[0]))
        rooms_in_use = active_rooms[:]

        server_code = JOIN_CHANNEL_CFG.get(parent_id, {}).get("server", "en")

        while len(rooms_in_use) < len(groups):
            new_rid, new_info = await self._create_room(
                join_channel, server_code=server_code
            )
            rooms_in_use.append((new_rid, new_info))

        # Si sobran rooms, borra las extras (si están vacías o las vaciamos)
        if len(rooms_in_use) > len(groups):
            extras = rooms_in_use[len(groups) :]
            rooms_in_use = rooms_in_use[: len(groups)]
            for rid, info in extras:
                # vaciar si tenía gente (no debería en este esquema, pero por seguridad)
                for m in list(info.get("players", [])):
                    try:
                        await self.bot.get_channel(info["join_channel_id"]).get_thread(
                            info["thread"]
                        ).remove_user(m)
                    except Exception:
                        pass
                info["players"] = []
                try:
                    await self.bot.get_channel(info["join_channel_id"]).get_thread(
                        info["thread"]
                    ).delete(reason="Extra queue room after rebalance")
                except Exception:
                    pass
                self.rooms.pop(rid, None)

        # Sincronizar miembros por room y renombrar room-1..N
        for idx, ((rid, info), desired) in enumerate(
            zip(rooms_in_use, groups), start=1
        ):
            info["display_num"] = idx
            th: discord.Thread = self.bot.get_channel(
                info["join_channel_id"]
            ).get_thread(info["thread"])
            try:
                await th.edit(name=f"room-{idx}")
            except Exception:
                pass

            current = list(info.get("players", []))
            cur_ids = {m.id for m in current}
            des_ids = {m.id for m in desired}

            # Remove primero
            for m in current:
                if m.id not in des_ids:
                    await self._remove_member_from_room(m, rid, info)

            # Add luego (manteniendo el orden desired)
            for m in desired:
                if m.id not in cur_ids:
                    await self._add_member_to_room(m, rid, info)

            # Reorden interno (lista) para reflejar el orden MMR
            info["players"] = desired[:]
            await self.update_room_board(guild, rid)

    async def _announce_need_players_prep_minute(
        self, guild: discord.Guild, join_channel: discord.TextChannel, now: dt.datetime
    ):
        """
        En ventana PREP (:x5 a :x9) manda el mismo "Need (n)" cada minuto.
        En :x0 no manda nada (porque ya no estamos en PREP).
        """
        join_id = join_channel.id

        prep_start = self._in_prep_window(now)  # 55 / 15 / 35 o None
        if prep_start is None:
            return

        # Gate: 1 vez por minuto por canal
        minute_key = (now.year, now.month, now.day, now.hour, now.minute)
        if self._need_last_minute.get(join_id) == minute_key:
            return
        self._need_last_minute[join_id] = minute_key

        ordered = await self._get_ordered_queue_members(guild, join_id)
        if not ordered:
            return

        rem = len(ordered) % ROOM_CAPACITY
        need = 0 if rem == 0 else (ROOM_CAPACITY - rem)

        # Recomendación práctica: evitar spam de "Need (5)" cuando no hay nadie
        # (si quieres mandarlo también cuando need == 5, quita el "len(ordered) < ROOM_CAPACITY" y deja need > 0)
        if need <= 0:
            return
        if need == ROOM_CAPACITY and len(ordered) < ROOM_CAPACITY:
            # cola < 5 -> need=5; normalmente no se desea spamear eso cada minuto
            return

        try:
            await join_channel.send(f"Need ({need}) player(s) to start the next room.")
        except Exception:
            pass

    async def _announce_room_start(
        self, join_channel: discord.TextChannel, rid: int, info: dict
    ):
        disp = self._disp(info, rid)
        players = info.get("players", [])
        # MMR para listar
        mmr_map = await self._fetch_mmrs_batch(
            [m.id for m in players if not self._is_placement_member(m)]
        )
        lines = []
        for m in players:
            if self._is_placement_member(m):
                lines.append(f"- {m.display_name} (Placement)")
            else:
                lines.append(f"- {m.display_name} ({mmr_map.get(m.id, 0)} MMR)")
        block = "\n".join(lines) if lines else "(no players)"
        await join_channel.send(f"Starting room {disp} with players:\n{block}")

    def _rooms_for_parent(self, parent_id: int) -> list[tuple[int, dict]]:
        """
        Salas activas (no finished) asociadas a un join_channel (parent_id).
        Nota: info["thread"] se guarda como thread_id (int), no como objeto Thread.
        Por eso la forma correcta de agrupar es por join_channel_id.
        """
        out: list[tuple[int, dict]] = []
        for rid, info in self.rooms.items():
            if info.get("finished", False):
                continue
            if int(info.get("join_channel_id") or 0) == int(parent_id):
                out.append((rid, info))
        return out

    def _queue_rooms(self, parent_id: int) -> list[tuple[int, dict]]:
        """
        Cola de salas NO iniciadas (started=False), ordenadas por display_num
        (room-1, room-2, ...) como fuente de verdad.

        display_num lo recalculamos con _reindex_parent_rooms (orden por avg MMR).
        """
        items = []
        for rid, info in self._rooms_for_parent(parent_id):
            if info.get("started", False):
                continue
            items.append((rid, info))

        # display_num primero; si falta, cae a created_ts
        items.sort(
            key=lambda t: (
                int(t[1].get("display_num") or 999999),
                int(t[1].get("created_ts", 0)),
                t[0],
            )
        )
        return items

    def _current_room(self, parent_id: int) -> tuple[int, dict] | tuple[None, None]:
        q = self._queue_rooms(parent_id)
        return q[0] if q else (None, None)

    async def _pick_highest_mmr_member(
        self, players: list[discord.Member]
    ) -> discord.Member | None:
        """
        Devuelve el miembro con MMR más alto (determinista por orden de join en empate).
        """
        if not players:
            return None
        mmr_map = await self._room_mmrs(players)
        # empate -> el que entró antes (índice menor) gana
        return max(players, key=lambda m: (mmr_map.get(m.id, 0), -players.index(m)))

    async def _rebalance_upwards(self, guild: discord.Guild, parent_id: int):
        """
        Si una sala superior (room-1, room-2, ...) queda con hueco (<5) y NO ha iniciado,
        sube al jugador con más MMR de la sala inmediata inferior para rellenar.
        Puede cascada.

        Regla: solo considera salas NO started y NO finished.
        """
        # Construimos lista actual de salas no-started ordenadas por avg MMR (como reindex)
        rooms = []
        for rid, info in self._rooms_for_parent(parent_id):
            if info.get("finished", False):
                continue
            if info.get("started", False):
                continue
            players = info.get("players", [])
            avg = await self._avg_mmr(players) if players else -1
            rooms.append((rid, info, int(avg), int(info.get("created_ts", 0))))

        # Más alto avg primero; desempates por created_ts y rid
        rooms.sort(key=lambda t: (-t[2], t[3], t[0]))

        if len(rooms) <= 1:
            return

        # Cascada: i toma de i+1 si tiene hueco
        for i in range(len(rooms) - 1):
            rid_up, info_up, *_ = rooms[i]

            if info_up.get("finished", False) or info_up.get("started", False):
                continue

            while len(info_up.get("players", [])) < ROOM_CAPACITY:
                rid_down, info_down, *_ = rooms[i + 1]

                if info_down.get("finished", False) or info_down.get("started", False):
                    break

                down_players = info_down.get("players", [])
                if not down_players:
                    break

                donor = await self._pick_highest_mmr_member(down_players)
                if donor is None:
                    break

                # mover donor: abajo -> arriba
                await self._remove_member_from_room(donor, rid_down, info_down)

                # cancelar countdown si abajo estaba STARTING y ya no está full
                if (
                    info_down.get("starting", False)
                    and len(info_down.get("players", [])) < ROOM_CAPACITY
                ):
                    await self._cancel_countdown_if_any(
                        info_down, reason="Countdown canceled (room is no longer full)."
                    )

                await self._add_member_to_room(donor, rid_up, info_up)

                # si arriba queda full, asegurar countdown
                await self._ensure_countdown(rid_up, info_up)

                # si abajo se quedó vacío, lo borramos y cortamos (no hay más para donar)
                if not info_down.get("players", []):
                    try:
                        down_parent = self.bot.get_channel(info_down["join_channel_id"])
                        if down_parent and isinstance(down_parent, discord.TextChannel):
                            down_thread = down_parent.get_thread(info_down["thread"])
                            if down_thread:
                                try:
                                    await down_thread.edit(archived=True, locked=True)
                                except Exception:
                                    pass
                                try:
                                    await down_thread.delete(reason="Empty room after rebalance")
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    self.rooms.pop(rid_down, None)
                    break


        # Al final: reindex/renombrar para reflejar el nuevo orden
        await self._reindex_parent_rooms(guild, parent_id)

    def _next_start_minute_label(self, now: dt.datetime) -> str:
        """
        Devuelve el próximo ':x0' según tus ventanas START.
        START_WINDOWS = ((0,4),(30,34)) -> minutos base: 0,30
        """
        starts = [a for (a, _b) in self.START_WINDOWS]  # [0,20,40]
        for s in starts:
            if s > now.minute:
                return f":{s:02d}"
        return f":{starts[0]:02d}"  # siguiente hora

    def _next_slot_dt_local(self, now: dt.datetime) -> dt.datetime:
        # START_WINDOWS = ((0,4),(20,24),(40,44)) -> bases: 0, 20, 40
        bases = sorted({a for (a, _b) in self.START_WINDOWS})
        base = now.replace(second=0, microsecond=0)
        for m in bases:
            if m > base.minute:
                return base.replace(minute=m)
        # next hour
        return (base + dt.timedelta(hours=1)).replace(minute=bases[0])


    async def build_rooms_overview_text(self, guild: discord.Guild, join_channel_id: int) -> str:
        """
        Texto para #rooms:
        - Lista rooms STARTED (con timestamp)
        - Luego lista FUTURE ROOMS (desde la queue /c), agrupadas de 5 y con huecos
        """
        now = self._now_lima()

        # ---------- STARTED ROOMS (threads existentes) ----------
        started_rooms: list[tuple[int, dict]] = []
        for rid, info in self._rooms_for_parent(join_channel_id):
            if info.get("finished", False):
                continue
            if info.get("phase") == PHASE_STARTED or info.get("started", False):
                started_rooms.append((rid, info))

        started_rooms.sort(key=lambda t: int(t[1].get("display_num") or 999999))

        # batch mmr para players de rooms started
        started_member_ids: list[int] = []
        for _rid, info in started_rooms:
            for m in info.get("players", []) or []:
                if m and not m.bot and not self._is_placement_member(m):
                    started_member_ids.append(m.id)

        started_mmr_map = await self._fetch_mmrs_batch(list(set(started_member_ids)))

        lines: list[str] = []

        for rid, info in started_rooms:
            disp = self._disp(info, rid)
            started_at = info.get("started_at")
            ts_part = f"<t:{int(started_at)}:T>" if started_at else "—"

            lines.append(f"ROOM {disp} STARTED {ts_part}")

            players = [m for m in (info.get("players", []) or []) if m and not m.bot]
            rows = []
            for m in players:
                if self._is_placement_member(m):
                    rows.append((m.display_name, -1, True))
                else:
                    rows.append((m.display_name, int(started_mmr_map.get(m.id, 0)), False))

            # non-placement primero, luego placement; MMR desc
            rows.sort(key=lambda x: (x[2], -x[1], x[0].lower()))
            for name, mmr, is_pl in rows:
                if is_pl:
                    lines.append(f"{name} Placement")
                else:
                    lines.append(f"{name} {mmr}MMR")
            lines.append("")  # blank line entre rooms

        # ---------- FUTURE ROOMS (desde la QUEUE /c; sin thread) ----------
        ordered = await self._get_ordered_queue_members(guild, join_channel_id)
        if ordered:
            future_slot = self._next_slot_dt_local(now)
            future_slot_ts = int(future_slot.timestamp())

            lines.append("----------------")
            lines.append(f"FUTURE ROOMS (next slot <t:{future_slot_ts}:T>)")

            mmr_map = await self._fetch_mmrs_batch(
                [m.id for m in ordered if not self._is_placement_member(m)]
            )

            groups = [ordered[i:i + ROOM_CAPACITY] for i in range(0, len(ordered), ROOM_CAPACITY)]
            for idx, g in enumerate(groups, start=1):
                lines.append(f"ROOM {idx}")
                for m in g:
                    if self._is_placement_member(m):
                        lines.append(f"{m.display_name} Placement")
                    else:
                        lines.append(f"{m.display_name} {int(mmr_map.get(m.id, 0))}MMR")

                # huecos visuales hasta 5
                for _ in range(ROOM_CAPACITY - len(g)):
                    lines.append("")

                lines.append("")

        return "\n".join(lines).rstrip()

            

    async def build_future_room_block(
        self, guild: discord.Guild, join_channel_id: int, now: dt.datetime
    ) -> str:
        """
        Bloque para que el cog Rooms lo agregue debajo de todas las salas:
        ----------------- Future Room (starting at :x0)
        u1 (mmr)
        u2 (mmr)
        """
        ordered = await self._get_ordered_queue_members(guild, join_channel_id)
        if not ordered:
            return ""

        mmr_map = await self._fetch_mmrs_batch(
            [m.id for m in ordered if not self._is_placement_member(m)]
        )

        label = self._next_start_minute_label(now)
        lines = [f"----------------- Future Room (starting at {label})"]
        for m in ordered:
            if self._is_placement_member(m):
                lines.append(f"{m.display_name} (Placement)")
            else:
                lines.append(f"{m.display_name} ({int(mmr_map.get(m.id, 0))})")

        return "\n".join(lines)

    async def _room_mmrs(self, players: list[discord.Member]) -> dict[int, int]:
        """MMR por user_id (1 query por jugador; si quieres, luego lo optimizamos por batch)."""
        mmr_map: dict[int, int] = {}
        for m in players:
            mmr, _ = await self.fetch_player(m.id)
            mmr_map[m.id] = int(mmr)
        return mmr_map

    async def _room_cutoff_and_lowest(self, info: dict) -> tuple[int, discord.Member]:
        """Cutoff = min MMR, lowest_member = uno de los mínimos (determinista por orden de join)."""
        players = info.get("players", [])
        mmr_map = await self._room_mmrs(players)
        # determinista: primero en la lista con min mmr
        lowest = min(players, key=lambda m: (mmr_map.get(m.id, 0), players.index(m)))
        cutoff = mmr_map.get(lowest.id, 0)
        return int(cutoff), lowest

    async def _avg_mmr(self, players: list[discord.Member]) -> int:
        if not players:
            return 0
        mmr_map = await self._room_mmrs(players)
        vals = [mmr_map.get(m.id, 0) for m in players]
        return int(round(sum(vals) / len(vals))) if vals else 0

    async def _create_room(
        self,
        join_channel: discord.TextChannel,
        server_code: str,
        thread_name: str | None = None,
    ):
        if not thread_name:
            # fallback seguro
            dt_local = self._now_lima()
            thread_name = f"room {dt_local.strftime('%Y-%m-%d %H%M')}"

        thread = await join_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            auto_archive_duration=1440,
            invitable=True,  # 👈 importante si tu versión lo soporta
        )

        await asyncio.sleep(0.2)  # 👈 evita “thread not ready” en algunos servers

        info = {
            "thread": thread.id,
            "join_channel_id": join_channel.id,
            "server": server_code,
            "players": [],
            "phase": PHASE_FUTURE,  # FUTURE: thread listo, songs aún NO
            "locked_in_thread": True,  # si quieres que estén bloqueados desde PREP
            "started": False,
            "finished": False,
            "created_ts": int(self._now_lima().timestamp()),
        }
        rid = max(self.rooms.keys(), default=0) + 1
        self.rooms[rid] = info
        return rid, info

    async def _reindex_parent_rooms(self, guild: discord.Guild, parent_id: int):
        """
        Recalcula display_num por parent #join:
        - Primero NO started (incluye STARTING), luego started
        - Dentro de NO started: Average MMR DESC (más alto = room-1)
        - Desempates: created_ts asc, rid asc
        Además renombra thread a room-{display_num}.
        """
        items = self._rooms_for_parent(parent_id)

        scored: list[tuple[bool, int, int, int, dict]] = []
        # tupla: (started, -avg, created_ts, rid, info)
        for rid, info in items:
            started = bool(info.get("started", False))
            created = int(info.get("created_ts", 0))
            players = info.get("players", [])
            avg = await self._avg_mmr(players) if players else -1
            scored.append((started, -int(avg), created, rid, info))

        scored.sort(key=lambda t: (1 if t[0] else 0, t[1], t[2], t[3]))

        for idx, (_started, _negavg, _created, rid, info) in enumerate(scored, start=1):
            info["display_num"] = idx
            th: discord.Thread = self.bot.get_channel(
                info["join_channel_id"]
            ).get_thread(info["thread"])
            try:
                await th.edit(name=f"room-{idx}")
            except Exception:
                pass

            # refrescar encabezado del tablero del thread
            try:
                await self.update_room_board(guild, rid)
            except Exception:
                pass

    def _disp(self, info: dict, fallback_rid: int) -> int:
        return int(info.get("display_num") or fallback_rid)

    async def _add_member_to_room(self, member: discord.Member, rid: int, info: dict):
        if member in info.get("players", []):
            return
        info["players"].append(member)
        try:
            await self.bot.get_channel(info["join_channel_id"]).get_thread(
                info["thread"]
            ).add_user(member)
        except discord.Forbidden as e:
            log.error(
                "add_user Forbidden in %s for %s (%s). Check Manage Threads/Create Private Threads perms.",
                info["thread"],
                member.id,
                member.display_name,
            )
            raise
        except discord.HTTPException as e:
            log.error(
                "add_user HTTPException in %s for %s: %r",
                info["thread"],
                member.id,
                e,
            )
            raise

    async def _remove_member_from_room(
        self, member: discord.Member, rid: int, info: dict
    ):
        if member not in info.get("players", []):
            return
        info["players"].remove(member)
        try:
            await self.bot.get_channel(info["join_channel_id"]).get_thread(
                info["thread"]
            ).remove_user(member)
        except Exception:
            pass
        await self.update_room_board(member.guild, rid)

    async def _cascade_insert(
        self,
        join_channel: discord.TextChannel,
        *,
        parent_id: int,
        incoming: discord.Member,
        server_code: str,
    ) -> tuple[int, dict]:
        """
        Inserta incoming en Waiting (Room2+), aplicando dominó:
        - Si room tiene espacio: entra
        - Si está llena:
            * incoming.mmr > cutoff => entra y expulsa al lowest hacia la siguiente
            * incoming.mmr == cutoff => no expulsa, incoming baja a la siguiente
            * incoming.mmr < cutoff => incoming baja a la siguiente
        Retorna (rid_final, info_final) donde quedó incoming.
        """
        incoming_mmr, _ = await self.fetch_player(incoming.id)

        # waiting rooms = cola SIN el current (Room2..)
        queue = self._queue_rooms(parent_id)
        if queue:
            queue = queue[1:]  # excluir current
        idx = 0

        while True:
            if idx >= len(queue):
                # crear nueva waiting room
                new_rid, new_info = await self._create_room(
                    join_channel, server_code=server_code
                )
                await self._add_member_to_room(incoming, new_rid, new_info)
                return new_rid, new_info

            rid, info = queue[idx]

            # si la sala se cerró/inició en el camino, saltar
            if info.get("finished", False) or info.get("started", False):
                idx += 1
                continue

            players = info.get("players", [])
            if len(players) < ROOM_CAPACITY:
                await self._add_member_to_room(incoming, rid, info)
                return rid, info

            cutoff, lowest = await self._room_cutoff_and_lowest(info)

            if int(incoming_mmr) > int(cutoff):
                # bump dentro de esta waiting room
                await self._remove_member_from_room(lowest, rid, info)
                await self._add_member_to_room(incoming, rid, info)
                incoming = lowest
                incoming_mmr, _ = await self.fetch_player(incoming.id)
                idx += 1
                continue

            # == o < : baja sin expulsar
            idx += 1
            continue

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
        exclude_titles: set[str] | None = None,
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
                    notin = ",".join(
                        f"${len(params)+i+1}" for i in range(len(exclude_titles))
                    )
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
                    *params,
                )

        out = []
        for r in rows:
            out.append(
                (
                    r["title"],
                    int(r["level"]),
                    r["diff"].capitalize(),
                    int(r["notes_count"]),
                )
            )
        return out

    async def _build_level_balanced_pool(
        self,
        low: int,
        high: int,
        *,
        server: str,
        diffs: tuple[str, ...],
        per_level: int = 5,
        exclude_titles: set[str] | None = None,
    ) -> list[tuple[str, int, str, int]]:
        """
        Construye un pool equilibrado por nivel:

        - Recorre cada nivel en [low, high].
        - Intenta sacar hasta `per_level` canciones por nivel usando `diffs`
          (por ejemplo: ('expert', 'master')).
        - Respeta `exclude_titles` y nunca duplica títulos.
        - Si en un nivel solo hay 1 canción, solo entra esa al pool.
        """
        pool: list[tuple[str, int, str, int]] = []
        used_titles: set[str] = set(exclude_titles or set())

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                for lvl in range(low, high + 1):
                    rows = await conn.fetch(
                        """
                        SELECT title, level, diff, notes_count
                        FROM songs
                        WHERE level = $1
                          AND server = $2
                          AND diff = ANY($3::text[])
                          AND title <> ALL($4::text[])
                        ORDER BY random()
                        LIMIT $5
                        """,
                        lvl,
                        server,
                        list(diffs),
                        list(used_titles) or ["__none__"],  # evitar array vacío
                        per_level,
                    )

                    for r in rows:
                        title = r["title"]
                        pool.append(
                            (
                                title,
                                int(r["level"]),
                                r["diff"].capitalize(),
                                int(r["notes_count"]),
                            )
                        )
                        used_titles.add(title)

        return pool

    async def _pick_placement_songs_levels_24_to_28(
        self, *, server: str = "en"
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
                        L,
                        server,
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
                            server,
                        )
                        for r in fb:
                            if r["title"] not in chosen_titles:
                                picked = r
                                break

                    if picked:
                        chosen_titles.add(picked["title"])
                        picks.append(
                            (
                                picked["title"],
                                int(picked["level"]),
                                picked["diff"].capitalize(),
                                int(picked["notes_count"]),
                            )
                        )

        random.shuffle(picks)
        return picks[:5]

    async def _resolve_season_id(
        self, season_choice: str | None
    ) -> tuple[int | None, str]:
        """
        Devuelve (season_id, season_name) según la elección del usuario.
        - None o "current/actual" => temporada activa (get_current_season)
        - "Pre Season"                  => busca por nombre 'Pre Season' (case-insensitive)
        - "Alpha Test Season"                  => busca por nombre 'Alpha Test Season' (case-insensitive)
        - "Beta Test Season"             => busca por nombre 'Beta Test Season' (case-insensitive)
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
            "pre season": ["pre season"],
            "alpha test season": ["alpha test season"],
            "beta test season": ["beta test season"],
        }
        candidate_names = aliases.get(key, [val])

        async with self.db_pool.acquire() as conn:
            # Intento por nombres (case-insensitive), priorizando la más reciente
            where = " OR ".join(
                f"LOWER(name) = ${i+1}" for i in range(len(candidate_names))
            )
            row = await conn.fetchrow(
                f"SELECT id, name FROM seasons WHERE {where} ORDER BY startdate DESC LIMIT 1",
                *[c.lower() for c in candidate_names],
            )

            if not row:
                # Si el usuario pasó un ID numérico
                if key.isdigit():
                    row = await conn.fetchrow(
                        "SELECT id, name FROM seasons WHERE id = $1", int(key)
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

            pos = int(row["pos"])
            total = int(row["total"])
            mmr = int(row["mmr"])
            role = row["role"]

            # Total de partidas jugadas en la season
            matches_played = (
                await conn.fetchval(
                    """
                SELECT COUNT(*)
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                """,
                    season_id,
                    user_id,
                )
                or 0
            )

            # Últimas 10 partidas (o menos si no hay tantas)
            recent_rows = await conn.fetch(
                """
                SELECT is_win, mmr_delta
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                ORDER BY played_at DESC, id DESC
                LIMIT 10
                """,
                season_id,
                user_id,
            )

            last10_wins = sum(1 for r in recent_rows if r["is_win"])
            last10_losses = sum(1 for r in recent_rows if not r["is_win"])
            last10_delta = (
                sum(int(r["mmr_delta"]) for r in recent_rows) if recent_rows else 0
            )

            # Peak MMR dentro de la season (máximo MMR alcanzado en cualquier momento)
            peak_mmr = await conn.fetchval(
                """
                SELECT GREATEST(MAX(mmr_before), MAX(mmr_after))
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                """,
                season_id,
                user_id,
            )

            # Si no tiene histórico, usamos el MMR actual
            if peak_mmr is None:
                peak_mmr = mmr

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

    async def _render_room_board_text(
        self, guild: discord.Guild, rid: int, capacity: int = 5
    ) -> str:
        info = self.rooms.get(rid)
        if not info:
            return ""

        players = info.get("players", [])

        rows = []
        for m in players:
            mmr, _ = await self.fetch_player(m.id)
            rows.append({"user_id": m.id, "mmr": int(mmr)})
        rows.sort(key=lambda r: r["mmr"], reverse=True)

        avg = round(sum(r["mmr"] for r in rows) / len(rows)) if rows else 0

        started = bool(info.get("started", False))
        starting = bool(info.get("starting", False))
        finished = bool(info.get("finished", False))

        if finished:
            status = "[FINISHED]"
        elif started:
            started_at = info.get("started_at")
            ts_part = f" <t:{int(started_at)}:T>" if started_at else ""
            status = f"[STARTED]{ts_part}"
        elif starting:
            dl = info.get("start_deadline_ts")
            ts_part = f" <t:{int(dl)}:R>" if dl else ""
            status = f"[STARTING]{ts_part}"
        else:
            status = "[NOT STARTED]"

        disp = self._disp(info, rid)
        header = f"Room {disp}: {len(rows)}/{capacity} Players · Average MMR {avg} · {status}"

        lines = []
        for r in rows:
            name = await self.resolve_member_name(guild, r["user_id"])
            lines.append(f"• {name} ({r['mmr']})")

        ts = int(datetime.datetime.utcnow().timestamp())
        return (
            header
            + ("\n" + "\n".join(lines) if lines else "")
            + f"\n\nLast Update <t:{ts}:R>"
        )

    async def close_room(
        self, thread: discord.Thread, *, delay: int = 60, delete: bool = True
    ):
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
            rid = next(
                (
                    rid
                    for rid, info in self.rooms.items()
                    if info["thread"] == thread.id
                ),
                None,
            )
            if rid is not None:
                self.rooms.pop(rid, None)
                try:
                    self.bot.dispatch("room_finished", rid)
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
                top_n - 1,  # OFFSET es 0-based
            )

        return int(row["mmr"]) if row else None

    def _format_cutoffs_board(
        self, season_name: str, data: dict[int, int | None]
    ) -> str:
        """
        data: {10: mmr_T10, 20: mmr_T20, ...}
        Devuelve un string listo para enviar en Discord con formato tipo tabla.
        """
        lines: list[str] = []
        lines.append(f"MMR cutoffs — Season {season_name}")
        lines.append("```")
        # Antes: "Tier | Players |  MMR"
        #       "-----+---------+------"
        lines.append("Tier |   MMR")
        lines.append("-----+------")

        # Ordenar T10, T20, T30, ...
        for n in sorted(data.keys()):
            mmr = data[n]
            tier_label = f"T{n}"
            mmr_str = str(mmr) if mmr is not None else "N/A"
            # Antes: f"{tier_label:<4}| {n:>7} | {mmr_str:>6}"
            lines.append(f"{tier_label:<4}| {mmr_str:>6}")

        lines.append("```")
        return "\n".join(lines)

    async def update_room_board(self, guild: discord.Guild, rid: int):
        info = self.rooms.get(rid)
        if not info:
            return
        # No intentes refrescar tableros de salas terminadas
        if info.get("finished", False):
            return

        thread: discord.Thread = self.bot.get_channel(
            info["join_channel_id"]
        ).get_thread(info["thread"])
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
                self.bot.dispatch("room_finished", dead_rid)
        except Exception:
            log.exception("update_room_board falló para rid=%s", rid)

    async def refresh_rooms_board_for_guild(self, guild: discord.Guild):
        """Re-renderiza todos los rooms del guild (para cambios de nombres)."""
        for rid, info in list(self.rooms.items()):
            if (
                self.bot.get_channel(info["join_channel_id"])
                .get_thread(info["thread"])
                .guild.id
                == guild.id
            ):
                await self.update_room_board(guild, rid)

    @staticmethod
    def _range_for_counts(counts: dict[str, int]) -> tuple[int, int]:
        return dynamic_range(counts)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._songs_lock = asyncio.Lock()
        self.queues = {cid: set() for cid in ALLOWED_JOIN_CHANNELS}
        self._slot_last_key = {}
        self._need_last_minute = {}
        self.room_slot_scheduler.start()
        self._prep_last_minute = {}  # join_id -> (y,m,d,h,min) gate para PREP
        self._queue_counter = 0
        self.queue_join_order: dict[int, dict[int, int]] = {
            cid: {} for cid in ALLOWED_JOIN_CHANNELS
        }

        # ✅ AGREGA ESTO:
        self._join_lock = asyncio.Lock()

        self.rooms: dict[int, dict] = {}
        try:
            if os.path.isfile("assets/cached_rooms.json"):
                with open("assets/cached_rooms.json", "r", encoding="utf-8") as f:
                    self.rooms = json.load(f)
        except:
            pass
        self._sched_last_minute = {}  # parent_id -> minute_ts
        self._sched_last_slot = {}  # parent_id -> (y,m,d,hm)

    def _need_players_window(self, dt_local: datetime) -> bool:
        return dt_local.minute in (1, 2, 3, 4, 31, 32, 33, 34)

    async def _need_players_count_for_join(
        self, guild: discord.Guild, join_id: int
    ) -> int:
        ordered = await self._get_ordered_queue_members(guild, join_id)
        if not ordered:
            return 0
        rem = len(ordered) % ROOM_CAPACITY
        return 0 if rem == 0 else (ROOM_CAPACITY - rem)

    @tasks.loop(seconds=5.0)
    async def room_slot_scheduler(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        now = self._now_lima()

        for join_id in list(ALLOWED_JOIN_CHANNELS):
            join_channel = guild.get_channel(join_id)
            if not isinstance(join_channel, discord.TextChannel):
                continue

            async with self._join_lock:
                # ---------- PREP (ventana :x5..:x9) ----------
                # ---------- PREP (ventana :x5..:x9) ----------
                prep_start = self._in_prep_window(now)
                if prep_start is not None:
                    minute_key = (now.year, now.month, now.day, now.hour, now.minute)
                    if self._prep_last_minute.get(join_id) != minute_key:
                        self._prep_last_minute[join_id] = minute_key

                        # 1) crear threads para grupos completos y postear canciones
                        await self._prepare_rooms_for_join_channel(guild, join_channel)

                        # 3) NUEVO: countdown + timestamp al slot
                        await self._announce_queue_countdown_prep_minute(
                            guild, join_channel, now
                        )

                # ---------- START (ventana :x0..:x4) ----------
                start_base = self._in_start_window(now)
                if start_base is not None:
                    # Ejecutar 1 vez por slot (00 / 20 / 40) por canal
                    slot_key = (now.year, now.month, now.day, now.hour, start_base)
                    if self._sched_last_slot.get(join_id) != slot_key:
                        self._sched_last_slot[join_id] = slot_key

                        slot_dt = now.replace(
                            second=0, microsecond=0, minute=start_base
                        )
                        slot_ts = int(slot_dt.timestamp())
                        # (1) PURGE: saca a los que quedaron en grupo incompleto al llegar el slot
                        await self._purge_unfilled_queue_on_slot(guild, join_channel, now)

                        # (2) START: inicia rooms FUTURE programadas para este slot
                        await self._start_future_rooms_for_slot(join_id, slot_ts)

                        # (3) ANNOUNCE: publica los próximos horarios (con timestamps)
                        await self._announce_next_queue_windows_on_00(join_channel, now)

    @room_slot_scheduler.before_loop
    async def _before_room_slot_scheduler(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if isinstance(message.channel, Thread):
            room = next(
                (
                    r
                    for r in self.rooms.values()
                    if r.get("thread") and r["thread"] == message.channel.id
                ),
                None,
            )
            if not room:
                return

            allowed_ids = {m.id for m in room.get("players", []) if m}

            # Bloquear @everyone / @here y roles siempre
            if message.mention_everyone or message.role_mentions:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            # Permitir @usuarios solo si todos están en la sala
            if message.mentions:
                if any(u.id not in allowed_ids for u in message.mentions):
                    try:
                        await message.delete()
                    except Exception:
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

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # Solo nos importa el main guild
        if member.guild.id != GUILD_ID:
            return

        # =========================================================
        # (A) NUEVO: si se fue mientras estaba en queue → sacarlo + anunciar Dropped
        #     + best-effort: removerlo de rooms no-finished para evitar "ghosts"
        # =========================================================
        try:
            async with self._join_lock:
                # A1) Remover de cualquier queue (EN/JP) y anunciar en su canal
                for join_id in list(ALLOWED_JOIN_CHANNELS):
                    q = self.queues.get(join_id, set())
                    if member.id in q:
                        q.discard(member.id)

                        ch = member.guild.get_channel(join_id)
                        if isinstance(ch, discord.TextChannel):
                            try:
                                qcount = len(q)
                                await ch.send(
                                    f"(<@{member.id}>) Dropped the queue (`{qcount}`)"
                                )
                            except Exception:
                                pass

                # A2) Remover del listado de players de cualquier room activa (si estaba)
                #     (Esto evita que una sala quede "llena" con un usuario que ya no existe)
                affected_parent_ids: set[int] = set()

                for rid, info in list(self.rooms.items()):
                    if info.get("finished", False):
                        continue

                    players = info.get("players", []) or []
                    # players suelen ser discord.Member, pero comparamos por id por seguridad
                    hit = next(
                        (p for p in players if getattr(p, "id", None) == member.id),
                        None,
                    )
                    if not hit:
                        continue

                    # remover del array
                    try:
                        info["players"] = [
                            p for p in players if getattr(p, "id", None) != member.id
                        ]
                    except Exception:
                        # fallback ultra seguro
                        try:
                            players.remove(hit)
                        except Exception:
                            pass

                    # intentar remover del thread (best effort)
                    try:
                        th = info.get("thread")
                        if isinstance(th, discord.Thread):
                            await th.remove_user(member)
                    except Exception:
                        pass

                    # si el room estaba en countdown/starting y se cayó de 5, cancela countdown si existe
                    if (
                        info.get("starting", False)
                        and len(info.get("players", [])) < ROOM_CAPACITY
                    ):
                        try:
                            if hasattr(self, "_cancel_countdown_if_any"):
                                await self._cancel_countdown_if_any(
                                    info,
                                    reason="Countdown canceled (player left the server).",
                                )
                        except Exception:
                            pass

                    # si queda vacío y aún no empezó, elimina el thread y saca el room del dict
                    try:
                        th = info.get("thread")
                        if (
                            not info.get("started", False)
                            and len(info.get("players", [])) == 0
                            and isinstance(th, discord.Thread)
                        ):
                            try:
                                await th.delete(
                                    reason="Empty room after member left server"
                                )
                            except Exception:
                                pass
                            self.rooms.pop(rid, None)
                            try:
                                self.bot.dispatch("room_finished", rid)
                            except Exception:
                                pass
                            continue
                    except Exception:
                        pass

                    # refrescar tablero del room
                    try:
                        await self.update_room_board(member.guild, rid)
                    except Exception:
                        pass

                    # marcar parent para rebalance (si aplica)
                    try:
                        th = info.get("thread")
                        if isinstance(th, discord.Thread) and isinstance(
                            th.parent_id, int
                        ):
                            affected_parent_ids.add(th.parent_id)
                    except Exception:
                        pass

                    # notificar a Rooms cog
                    try:
                        self.bot.dispatch("room_updated", rid)
                    except Exception:
                        pass

                # A3) Si afectó algún parent (#join / #jp-join), intenta rebalance
                for parent_id in list(affected_parent_ids):
                    if parent_id in ALLOWED_JOIN_CHANNELS:
                        parent_ch = member.guild.get_channel(parent_id)
                        if isinstance(parent_ch, discord.TextChannel):
                            try:
                                # Rebalance en background para reordenar rooms y quitar huecos
                                asyncio.create_task(
                                    self._rebalance_parent_queue(
                                        member.guild, parent_ch
                                    )
                                )
                            except Exception:
                                pass

                # A4) refrescar #rooms global
                try:
                    self.bot.dispatch("room_updated", None)
                except Exception:
                    pass

        except Exception:
            # No bloqueamos el resto del cleanup si falla algo acá
            pass

        # =========================================================
        # (B) TU LÓGICA EXISTENTE: cleanup en DB (Placement ghosts)
        # =========================================================
        async with self.db_pool.acquire() as conn:
            # 1) Todas las seasons donde aparece en seasonplayers
            sp_rows = await conn.fetch(
                """
                SELECT season_id, mmr, role
                FROM seasonplayers
                WHERE user_id = $1
                """,
                member.id,
            )

            if not sp_rows:
                # Nunca estuvo registrado en ninguna season → no tocamos nada
                return

            # 2) Seasons donde tiene historial en match_history
            mh_rows = await conn.fetch(
                """
                SELECT DISTINCT season_id
                FROM match_history
                WHERE user_id = $1
                """,
                member.id,
            )
            mh_seasons = {r["season_id"] for r in mh_rows}

            # 3) Seasons "ghost": Placement, mmr=0 y sin historial
            never_played_seasons = [
                r["season_id"]
                for r in sp_rows
                if int(r["mmr"]) == 0
                and r["role"] == "Placement"
                and r["season_id"] not in mh_seasons
            ]

            # 4) Seasons "reales": cualquier cosa que NO sea ghost
            played_seasons = [
                r["season_id"]
                for r in sp_rows
                if not (
                    int(r["mmr"]) == 0
                    and r["role"] == "Placement"
                    and r["season_id"] not in mh_seasons
                )
            ]

            # 5) Borrar solo las seasons ghost de seasonplayers
            if never_played_seasons:
                await conn.execute(
                    """
                    DELETE FROM seasonplayers
                    WHERE user_id  = $1
                    AND season_id = ANY($2::int[])
                    """,
                    member.id,
                    never_played_seasons,
                )

            # 6) Si todavía tiene alguna season real o historial, NO tocamos players
            if mh_seasons or played_seasons:
                return

            # 7) Si llega aquí:
            #    - no tiene historial en match_history
            #    - y todas sus seasons eran ghost (Placement / 0 MMR sin partidas)
            #    → lo borramos completamente
            await conn.execute(
                "DELETE FROM seasonplayers WHERE user_id = $1",
                member.id,
            )
            await conn.execute(
                "DELETE FROM players WHERE user_id = $1",
                member.id,
            )

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
                    room_mmr    INTEGER,
                    songs_block TEXT,                      -- 👈 NUEVA COLUMNA
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # Por si la tabla ya existía sin songs_block, la añadimos
            await conn.execute(
                "ALTER TABLE results ADD COLUMN IF NOT EXISTS songs_block TEXT;"
            )

            # NEW: tabla de canciones por resultado para la web
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS match_songs (
                    id          BIGSERIAL PRIMARY KEY,
                    result_id   BIGINT       NOT NULL,
                    song_name   TEXT         NOT NULL,
                    level       INTEGER      NOT NULL,
                    difficulty  TEXT         NOT NULL,
                    notes_count INTEGER      NOT NULL,
                    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                );
                """
            )

            # 🔧 Asegurar tipos correctos aunque la tabla ya existiera antes
            await conn.execute(
                """
                ALTER TABLE match_songs
                    ALTER COLUMN level       TYPE INTEGER USING level::integer,
                    ALTER COLUMN notes_count TYPE INTEGER USING notes_count::integer;
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_match_songs_result
                ON match_songs (result_id);
                """
            )

            # Historial de partidas por jugador y temporada
            await conn.execute(
                """
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
            """
            )

            # Por si ya existía tabla vieja:
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS result_id BIGINT;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS pos INTEGER;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_perfect INTEGER;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_great INTEGER;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_good INTEGER;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_bad INTEGER;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS s_miss INTEGER;"
            )
            await conn.execute(
                "ALTER TABLE match_history ADD COLUMN IF NOT EXISTS was_placement BOOLEAN DEFAULT false;"
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_match_history_user_season
                ON match_history (user_id, season_id, played_at DESC);
            """
            )

            # Canciones (catálogo global). Si ya existe, no re-crea.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS songs (
                    id           INTEGER NOT NULL,
                    title        TEXT    NOT NULL,
                    diff         TEXT    NOT NULL,
                    level        INTEGER NOT NULL,
                    notes_count  INTEGER NOT NULL DEFAULT 0
                    -- OJO: sin server aquí o con server, da igual
                    -- si ya existe no toca la tabla.
                );
            """
            )

            # 1️⃣ Primero aseguro que la columna server exista
            await conn.execute(
                """
                ALTER TABLE songs
                ADD COLUMN IF NOT EXISTS server TEXT NOT NULL DEFAULT 'en';
            """
            )

            # 2️⃣ Ahora sí puedo borrar duplicados por (id,diff,server)
            await conn.execute(
                """
                DELETE FROM songs a
                USING songs b
                WHERE a.id = b.id
                  AND a.diff = b.diff
                  AND a.server = b.server
                  AND a.ctid > b.ctid;
            """
            )

            # 3️⃣ Unique en (id, diff, server) para EN/JP
            await conn.execute(
                """
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
            """
            )

            # Índices útiles
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_songs_level_server ON songs(level, server);"
            )

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
                    diffs_en = await grab(self.RAW_EN, "musicDifficulties")
                    bundles.append(("en", musics_en, diffs_en))
                except Exception as e:
                    log.warning(f"[songs] EN fetch failed ({e})")

                # JP
                try:
                    musics_jp = await grab(self.RAW_JP, "musics")
                    diffs_jp = await grab(self.RAW_JP, "musicDifficulties")
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
                        notes_total = sum(
                            v for v in notes_total.values() if isinstance(v, int)
                        )
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
                        notes_map = d.get("notes")
                        if isinstance(notes_map, dict):
                            notes_total = sum(
                                v for v in notes_map.values() if isinstance(v, int)
                            )

                    rows_for_server.append(
                        (
                            d["musicId"],
                            title,
                            d["musicDifficulty"],
                            int(d["playLevel"]),
                            int(notes_total or 0),
                            server_code,  # 'en' o 'jp'
                        )
                    )

                by_server[server_code] = rows_for_server

            async with self.db_pool.acquire() as conn:
                # Solo tocamos los servidores que sí pudimos refrescar en este ciclo
                for server_code, rows in by_server.items():
                    # Limpiamos catálogo viejo de ese servidor
                    await conn.execute(
                        "DELETE FROM songs WHERE server = $1", server_code
                    )

                    if not rows:
                        log.warning(
                            f"[songs] No hay canciones válidas para server={server_code} tras filtrar por release+1d."
                        )
                        continue

                    # Insertamos solo las canciones que YA cumplieron el día de gracia
                    await conn.executemany(
                        """
                        INSERT INTO songs (id, title, diff, level, notes_count, server)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        rows,
                    )

        print(
            "[songs] Catálogo EN/JP actualizado (reemplazo por servidor, con filtro release+1d)"
        )

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
                    low,
                    high,
                    *diffs,
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

    def _lockdown_window(self, now: dt.datetime) -> bool:
        # Lockdown exactamente durante PREP (:x5..:x9)
        return self._in_prep_window(now) is not None

    async def _need_players_next_room(self, guild: discord.Guild, join_id: int) -> int:
        ordered = await self._get_ordered_queue_members(guild, join_id)
        if not ordered:
            return ROOM_CAPACITY  # faltan 5 para la primera
        rem = len(ordered) % ROOM_CAPACITY
        if rem == 0:
            return 0
        return ROOM_CAPACITY - rem

    async def _is_join_locked(
        self, guild: discord.Guild, join_id: int, now: dt.datetime
    ) -> bool:
        """
        Regla pedida:
        - Solo durante PREP (:x5..:x9)
        - Si en :x5 ya están las salas completas (>= 1 grupo full)
        y NO hay otra sala que necesite jugadores (need==0),
        entonces se bloquea el join.
        - Si falta gente para completar (need>0), se permite entrar.
        """
        if not self._lockdown_window(now):
            return False

        ordered = await self._get_ordered_queue_members(guild, join_id)
        full_groups = len(ordered) // ROOM_CAPACITY
        rem = len(ordered) % ROOM_CAPACITY
        need = 0 if rem == 0 else (ROOM_CAPACITY - rem)

        # Caso clave: ya hay al menos 1 sala completa y no se necesita nadie -> LOCK
        if full_groups >= 1 and need == 0:
            return True

        # Si falta gente para completar la siguiente, permitir
        return False

    async def launch_song_poll(self, room_info):
        # Evitar re-selección si ya se postearon songs (más fuerte que started)
        if room_info.get("songs_posted") and not room_info.get("rerolling"):
            try:
                await self.bot.get_channel(room_info["join_channel_id"]).get_thread(
                    room_info["thread"]
                ).send("Songs already selected for this room.")
            except Exception:
                pass
            return

        # Si ya está started, también protege (redundante pero ok)
        if room_info.get("started") and not room_info.get("rerolling"):
            try:
                await self.bot.get_channel(room_info["join_channel_id"]).get_thread(
                    room_info["thread"]
                ).send("Match already started; songs were selected earlier.")
            except Exception:
                pass
            return

        thread = self.bot.get_channel(room_info["join_channel_id"]).get_thread(
            room_info["thread"]
        )
        players = room_info["players"]

        server_code = room_info.get("server", "en")

        # 1) Contar rangos (por rol primero, luego por MMR)
        counts = {rank: 0 for rank in BRACKET_RANGES}
        for m in players:
            # 1) Intentar detectar rango por rol
            rank_by_role = next(
                (r for r in BRACKET_RANGES if discord.utils.get(m.roles, name=r)), None
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
            # a) Rango base SOLO para non-append (según rango por MMR promedio)
            base_lo, base_hi = low, high  # p.ej. Master ⬆ → 31–35
            non_append_diffs = tuple(d for d in allowed_diffs if d != "append")

            # b) Rango de append independiente, basado en el mismo rango del room
            append_range = None
            if "append" in allowed_diffs:
                append_range = APPEND_RANGES.get(base_rank)  # p.ej. Master ⬆ → 29–32

            # c) Pools separados
            pool_non_append: list[tuple[str, int, str, int]] = []
            pool_append: list[tuple[str, int, str, int]] = []

            # Non-append (hard/expert/master) SIEMPRE en su rango base,
            # pero equilibrado por nivel (intentamos 5 por nivel).
            taken_titles: set[str] = set()
            if non_append_diffs:
                pool_non_append = await self._build_level_balanced_pool(
                    base_lo,
                    base_hi,
                    server=server_code,
                    diffs=non_append_diffs,
                    per_level=5,  # 5 canciones por nivel → 25 en total si el catálogo lo permite
                )
                taken_titles = {t for (t, *_rest) in pool_non_append}

            # Append SOLO en su rango de append, sin romper la probabilidad de append
            if append_range is not None:
                alo, ahi = append_range
                pool_append = await self._get_random_songs(
                    alo,
                    ahi,
                    ("append",),
                    limit=APPEND_POOL_LIMIT,
                    server=server_code,
                    exclude_titles=taken_titles,
                )

            # d) Selección final
            prob = get_append_probability_for_rank(base_rank)
            picks: list[tuple[str, int, str, int]] = []
            used_append = False

            # ¿Forzamos al menos 1 append?
            if pool_append and random.random() < prob:
                used_append = True
                random.shuffle(pool_append)
                random.shuffle(pool_non_append)

                append_count = pick_append_count_for_rank(
                    base_rank, max_available=len(pool_append)
                )
                append_count = max(1, min(append_count, 5))

                picks = []
                picks.extend(pool_append[:append_count])

                remaining = 5 - len(picks)

                # completar con non-append primero
                if remaining > 0 and pool_non_append:
                    picks.extend(pool_non_append[:remaining])
                    remaining = 5 - len(picks)

                # si aún falta, completar con más append
                if remaining > 0 and len(pool_append) > append_count:
                    picks.extend(pool_append[append_count : append_count + remaining])

            else:
                # Intentar solo non-append en su rango
                random.shuffle(pool_non_append)
                picks = pool_non_append[:5]
                remaining = 5 - len(picks)

                # Si no alcanza y hay append, usar append como fallback
                if remaining > 0 and pool_append:
                    used_append = True
                    random.shuffle(pool_append)
                    picks.extend(pool_append[:remaining])

            # Máximo 5 canciones
            final_picks = secrets.SystemRandom().sample(picks, k=5)

            # e) Decidir qué gap mostrar
            if used_append and append_range is not None:
                alo, ahi = append_range
                # Mostramos la unión de ambos rangos
                low = min(base_lo, alo)
                high = max(base_hi, ahi)
            else:
                # No hay append en la selección → gap SOLO de Master/Master ⬆
                low, high = base_lo, base_hi

        # 4) Construir el mensaje
        total_notes = sum(int(n or 0) for *_, n in final_picks)  # suma segura

        # Guardar info de la sala
        room_info["total_notes"] = total_notes
        room_info["gap_low"] = low
        room_info["gap_high"] = high
        room_info["songs"] = (
            final_picks  # 👈 NEW: guardamos las canciones seleccionadas
        )
        room_info["rerolling"] = False

        song_lines = "\n".join(
            f"{i}. **{title}** (Lv {lvl} {diff}) [{notes} notas]"
            for i, (title, lvl, diff, notes) in enumerate(final_picks, start=1)
        )

        msg = await thread.send(
            f"Selected songs (Lv {low}-{high})\n"
            f"(Total {total_notes} notes)\n"
            f"{song_lines}"
        )
        await msg.pin()

        # ——— MARCAR INICIO + TIMESTAMP ———
        room_info["songs_posted"] = True
        room_info["phase"] = PHASE_PREPARED
        room_info["locked_in_thread"] = True
        # started/started_ts se setean únicamente en :00/:30

        # refrescar el tablero del room y #rooms
        rid = next(
            (rid for rid, r in self.rooms.items() if r["thread"] == thread.id), None
        )
        if rid is not None:
            await self.update_room_board(thread.guild, rid)
            self.bot.dispatch(
                "room_updated", rid
            )  # ← para actualizar #rooms al instante

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
        if not season:
            return 0, "Placement"
        season_id = season["id"]

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT sp.mmr, sp.role
                FROM players p
                INNER JOIN seasonplayers sp ON p.user_id = sp.user_id
                WHERE p.user_id = $1
                AND sp.season_id = $2
                """,
                user_id,
                season_id,
            )

            if not row:
                # Fail-safe: evita crash cuando aún no existe registro
                return 0, "Placement"

            return int(row["mmr"]), str(row["role"])


    async def ensure_join_channel(self, guild: discord.Guild):
        for ch in guild.text_channels:
            if ch.name == JOIN_CHANNEL_NAME:
                return ch
        return await guild.create_text_channel(JOIN_CHANNEL_NAME)

    @app_commands.command(
        name="c",
        description="Join queue for the next room slot",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def join_room(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or ch.id not in ALLOWED_JOIN_CHANNELS:
            return await interaction.response.send_message(
                "This command only works on #join / #jp-join.", ephemeral=True
            )

        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Guild not found.", ephemeral=True)

        member: discord.Member = interaction.user  # type: ignore

        async with self._join_lock:
            # Guard: ya estás en una sala activa (cualquier fase no-finished)
            already_room = self._find_player_room_id(member)
            if already_room is not None:
                return await interaction.response.send_message(
                    "❌ You are already in a room. You cannot queue again until that room finishes.",
                    ephemeral=True,
                )

            # Guard: ya estás en cola (EN o JP)
            for cid in ALLOWED_JOIN_CHANNELS:
                if member.id in self.queues.get(cid, set()):
                    return await interaction.response.send_message(
                        "❌ You are already in the queue.", ephemeral=True
                    )

            # Lock-down: durante PREP se bloquea SOLO si need==0 (tu lógica actual)
            now = self._now_lima()
            if await self._is_join_locked(guild, ch.id, now):
                return await interaction.response.send_message(
                    "⛔ Queue is locked down until the next slot.", ephemeral=True
                )

            # Siempre encola (no se intenta meter a rooms “unfilled”)
            self.queues.setdefault(ch.id, set()).add(member.id)
            self._queue_counter += 1
            self.queue_join_order[ch.id][member.id] = self._queue_counter

            # Rol de queue según canal
            await self._assign_queue_role_for_join(member, ch.id)

            # Calcular posición informativa (orden determinístico)
            ordered = await self._get_ordered_queue_members(guild, ch.id)
            try:
                idx = ordered.index(member)
            except ValueError:
                idx = len(ordered) - 1

            room_no = (idx // ROOM_CAPACITY) + 1
            pos_in_room = (idx % ROOM_CAPACITY) + 1
            need = await self._need_players_next_room(guild, ch.id)

            # Slot informativo
            slot_dt = self._next_slot_dt_local(now)
            slot_ts = int(slot_dt.timestamp())

            # Notificar al cog Rooms para refrescar #rooms
            self.bot.dispatch("room_updated", None)

        # Respuesta (fuera del lock)
        await interaction.response.send_message(
            f"Queued. Future room **{room_no}** ({pos_in_room}/5). "
            f"{'Room complete.' if need == 0 else f'Need {need} more to complete the next room.'}\n"
            f"Next slot: <t:{slot_ts}:T> (<t:{slot_ts}:R>).",
            ephemeral=True,
        )

        # Mensaje público en #join / #jp-join
        try:
            qcount = len(self.queues.get(ch.id, set()))
            await ch.send(f"({member.mention}) Joined the queue (`{qcount}`)")
        except Exception:
            pass


    @app_commands.command(
        name="d",
        description="Drop queue in #join/#jp-join OR leave your current room thread",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def leave_queue(self, interaction: discord.Interaction):
        ch = interaction.channel
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Guild not found.", ephemeral=True)

        member: discord.Member = interaction.user  # type: ignore

        # ------------------------------------------------------------
        # CASO A: /d dentro de un ROOM THREAD -> salir de la sala actual
        # ------------------------------------------------------------
        if isinstance(ch, discord.Thread):
            async with self._join_lock:
                rid, info = self._find_room_by_thread_id(ch.id)
                if not info:
                    return await interaction.response.send_message(
                        "ℹ️ This thread is not an active room.", ephemeral=True
                    )

                if member not in (info.get("players", []) or []):
                    return await interaction.response.send_message(
                        "ℹ️ You are not listed as a player in this room.", ephemeral=True
                    )

                # Si ya empezó, no permitir /d (usa /forfeit según tu flujo)
                if info.get("phase") == PHASE_STARTED or info.get("started", False):
                    return await interaction.response.send_message(
                        "❌ This match already started. Use `/forfeit` if everyone agrees to close the room.",
                        ephemeral=True,
                    )

                # Remover del room + del thread
                await self._remove_member_from_room(member, int(rid), info)

                # Best-effort: quitar roles de queue (por si acaso)
                try:
                    await self._remove_queue_roles([member])
                except Exception:
                    pass

                # Mensaje público dentro del thread
                try:
                    await ch.send(f"({member.mention}) Left the room.")
                except Exception:
                    pass

                # Si queda vacío -> archivar/cerrar el hilo
                if len(info.get("players", []) or []) == 0:
                    try:
                        await ch.send("Room is empty. Archiving this thread.")
                    except Exception:
                        pass

                    # close_room ya saca del dict y dispara room_finished
                    try:
                        await self.close_room(ch, delay=0, delete=False)  # archive+lock
                    except Exception:
                        pass

                # Reindex visual (room-1, room-2, ...)
                try:
                    await self._reindex_parent_rooms(guild, int(info["join_channel_id"]))
                except Exception:
                    pass

                # Refrescar #rooms
                try:
                    self.bot.dispatch("room_updated", None)
                except Exception:
                    pass

            return await interaction.response.send_message("Removed from room.", ephemeral=True)

        # ------------------------------------------------------------
        # CASO B: /d en #join / #jp-join -> salir SOLO de la queue
        # ------------------------------------------------------------
        if not isinstance(ch, discord.TextChannel) or ch.id not in ALLOWED_JOIN_CHANNELS:
            return await interaction.response.send_message(
                "This command works in #join/#jp-join (drop queue) or inside a room thread (leave room).",
                ephemeral=True,
            )

        async with self._join_lock:
            q = self.queues.get(ch.id, set())
            if member.id not in q:
                return await interaction.response.send_message(
                    "ℹ️ You are not in the queue.", ephemeral=True
                )

            q.discard(member.id)
            self.queue_join_order.get(ch.id, {}).pop(member.id, None)

            try:
                await self._remove_queue_roles([member])
            except Exception:
                pass

            # Notificar al cog Rooms
            try:
                self.bot.dispatch("room_updated", None)
            except Exception:
                pass

            # Mensaje público
            try:
                qcount = len(q)
                await ch.send(f"({member.mention}) Dropped the queue (`{qcount}`)")
            except Exception:
                pass

        return await interaction.response.send_message("Removed from queue.", ephemeral=True)


    @app_commands.command(
        name="stats",
        description="Shows your position, mmr and rank in the selected season",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        season="Season: Current, Pre Season, Alpha Test Season or Beta Test Season"
    )
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Pre Season", value="pre season"),
            app_commands.Choice(name="Beta Test Season", value="beta test season"),
            app_commands.Choice(name="Alpha Test Season", value="alpha test season"),
        ]
    )
    async def stats(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message(
                "⚠️ Season not found", ephemeral=True
            )

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
        name="searchresult", description="Show a past results table by its global ID"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        result_id="Result ID shown under the season name in the results embed"
    )
    async def searchresult(self, interaction: discord.Interaction, result_id: int):
        # Buscar el result en la tabla
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT result_id, season_name, channel_id, message_id, gap_low, gap_high, created_at
                FROM results
                WHERE result_id = $1
                """,
                result_id,
            )

        if not row:
            return await interaction.response.send_message(
                f"⚠️ No result found with ID `{result_id}`."
            )

        channel = await self._resolve_channel(
            row["channel_id"], fallback=interaction.channel
        )
        try:
            msg = await channel.fetch_message(row["message_id"])
        except discord.NotFound:
            return await interaction.response.send_message(
                "⚠️ The original result message could not be found (maybe deleted)."
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⚠️ I don't have permissions to view the channel of that result."
            )

        content = (
            f"Result **#{row['result_id']}** — **{row['season_name']}**\n"
            f"[Jump to original message]({msg.jump_url})"
        )

        if msg.embeds:
            await interaction.response.send_message(content, embed=msg.embeds[0])
        else:
            await interaction.response.send_message(content)

    @app_commands.command(
        name="stats_user",
        description="Shows your position, mmr and rank in the selected season",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        user="Player",
        season="Season: Current, Pre Season, Alpha Test Season or Beta Test Season",
    )
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Pre Season", value="pre season"),
            app_commands.Choice(name="Beta Test Season", value="beta test season"),
            app_commands.Choice(name="Alpha Test Season", value="alpha test season"),
        ]
    )
    async def stats_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        season: app_commands.Choice[str] | None = None,
    ):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message(
                "⚠️ No encontré la temporada seleccionada.", ephemeral=True
            )

        data = await self._fetch_user_stats(user.id, season_id)
        if not data:
            player_name = await self.resolve_member_name(interaction.guild, user.id)
            return await interaction.response.send_message(
                f"{player_name} doesn't have register in **Season {season_name}**.",
                ephemeral=True,
            )

        mmr_delta_last10 = data["last10_mmr_delta"]
        mmr_delta_str = f"{mmr_delta_last10:+d}"

        player_name = await self.resolve_member_name(interaction.guild, user.id)

        msg = (
            f"**Stats — Season {season_name}**\n"
            f"• **Player:** {player_name}\n"
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
        description="Shows the MMR Cutoff of T10, T20, T30, T40, T50 y T100 for the selected season",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        season="Temporada: Current, Pre Season, Alpha Test Season or Beta Test Season"
    )
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Pre Season", value="pre season"),
            app_commands.Choice(name="Beta Test Season", value="beta test season"),
            app_commands.Choice(name="Alpha Test Season", value="alpha tests season"),
        ]
    )
    async def cutoffs(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message(
                "⚠️ No encontré la temporada seleccionada.", ephemeral=True
            )

        # calcular cutoffs
        tiers = (10, 20, 30, 40, 50, 100)
        data = {}
        for n in tiers:
            data[n] = await self._fetch_cutoff(season_id, n)

        board = self._format_cutoffs_board(season_name, data)
        await interaction.response.send_message(board)


    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="mmr", description="Muestra tu MMR actual")
    async def mmr_self(self, interaction: discord.Interaction):
        mmr_val, role = await self.fetch_player(interaction.user.id)
        name = interaction.user.display_name
        if role == "Placement":
            await interaction.response.send_message(f"{name} Is in placement")
        else:
            await interaction.response.send_message(
                f"{name} has {mmr_val} MMR and rank {role}."
            )

    @app_commands.command(
        name="top100", description="Top 100 jugadores por MMR con paginación"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        season="Season: Current, Pre Season, Alpha Test Season or Beta Test Season"
    )
    @app_commands.choices(
        season=[
            app_commands.Choice(name="Current", value="current"),
            app_commands.Choice(name="Pre Season", value="pre season"),
            app_commands.Choice(name="Beta Test Season", value="beta test season"),
            app_commands.Choice(name="Alpha Test Season", value="alpha test season"),
        ]
    )
    async def top100(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        # Resolver season (igual que en /stats, /cutoffs, etc.)
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.response.send_message(
                "⚠️ Season not found.", ephemeral=True
            )

        view = Top100View(self.db_pool, interaction, season_id, season_name)

        total = await self.db_pool.fetchval(
            """
            SELECT COUNT(*)
            FROM seasonplayers
            WHERE season_id = $1
            """,
            season_id,
        )
        max_page = (total - 1) // view.per_page if total else 0
        view.prev_page.disabled = True
        view.next_page.disabled = 0 == max_page

        embed = await view.build_embed()
        await interaction.response.send_message(embed=embed, view=view)

    @commands.command(name="cleanup_ghosts")
    @commands.has_permissions(administrator=True)
    async def cleanup_ghosts(self, ctx: commands.Context):
        """
        Limpia usuarios que ya no están en el servidor:

        - Si nunca jugaron en ninguna season (solo mmr=0/Placement y sin match_history):
          se eliminan de seasonplayers y players (desaparecen del leaderboard).
        - Si sí jugaron en alguna season:
          se eliminan solo de las seasons donde están como mmr=0/Placement
          y sin historial (seasons donde se registraron pero nunca jugaron).
        """
        guild = ctx.guild or self.bot.get_guild(GUILD_ID)
        if guild is None:
            return await ctx.send("No puedo encontrar el servidor principal.")

        # Tomamos todos los user_id que aparecen en seasonplayers
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM seasonplayers")
            user_ids = [r["user_id"] for r in rows]

        removed_full = 0
        cleaned_seasons = 0

        for uid in user_ids:
            # Si el jugador aún está en el servidor, NO lo tocamos
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                member = None
            except discord.HTTPException:
                member = guild.get_member(uid)

            if member is not None:
                continue  # sigue en el servidor, lo dejamos

            # Aquí: usuario que YA NO está en el servidor
            async with self.db_pool.acquire() as conn:
                sp_rows = await conn.fetch(
                    "SELECT season_id, mmr, role FROM seasonplayers WHERE user_id = $1",
                    uid,
                )
                if not sp_rows:
                    # Por si quedó colgado solo en players
                    await conn.execute(
                        "DELETE FROM players WHERE user_id = $1",
                        uid,
                    )
                    continue

                mh_rows = await conn.fetch(
                    "SELECT DISTINCT season_id FROM match_history WHERE user_id = $1",
                    uid,
                )
                mh_seasons = {r["season_id"] for r in mh_rows}

                never_played_seasons = [
                    r["season_id"]
                    for r in sp_rows
                    if r["mmr"] == 0
                    and r["role"] == "Placement"
                    and r["season_id"] not in mh_seasons
                ]

                played_seasons = [
                    r["season_id"]
                    for r in sp_rows
                    if not (
                        r["mmr"] == 0
                        and r["role"] == "Placement"
                        and r["season_id"] not in mh_seasons
                    )
                ]

                # 1) Borramos seasons donde nunca jugó
                if never_played_seasons:
                    await conn.execute(
                        """
                        DELETE FROM seasonplayers
                        WHERE user_id  = $1
                          AND season_id = ANY($2::int[])
                        """,
                        uid,
                        never_played_seasons,
                    )
                    cleaned_seasons += len(never_played_seasons)

                # 2) Si no le queda ninguna season real ni historial:
                #    jugador fantasma → borrar completamente
                if not mh_seasons and not played_seasons:
                    await conn.execute(
                        "DELETE FROM seasonplayers WHERE user_id = $1",
                        uid,
                    )
                    await conn.execute(
                        "DELETE FROM players WHERE user_id = $1",
                        uid,
                    )
                    removed_full += 1

        await ctx.send(
            f"✅ Limpieza completada.\n"
            f"- Usuarios eliminados completamente: {removed_full}\n"
            f"- Registros de seasons eliminados (Placement 0 MMR sin historial): {cleaned_seasons}"
        )

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
            "🎵 Canciones aleatorias:\n"
            + "\n".join(
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
                    next_id,
                )
            else:
                next_id = int(row["value"]) + 1
                await conn.execute(
                    "UPDATE global_settings SET value = $1 WHERE key = 'last_result_id'",
                    next_id,
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
        if 961 <= other <= 1000:
            return 100, "Bronze"
        if 1001 <= other <= 1100:
            return 95, "Iron ⬆"
        if 1101 <= other <= 1200:
            return 90, "Iron ⬆"
        if 1201 <= other <= 1300:
            return 80, "Iron ⬆"
        if 1301 <= other <= 1400:
            return 70, "Iron ⬆"
        if 1401 <= other <= 1500:
            return 60, "Iron ⬆"
        if 1501 <= other <= 1600:
            return 50, "Iron"
        if 1601 <= other <= 1700:
            return 40, "Iron"
        if 1701 <= other <= 1800:
            return 30, "Iron"
        if 1801 <= other <= 1900:
            return 20, "Iron"
        if 1901 <= other <= 2000:
            return 10, "Iron"

        # ⬇️ Cualquier cosa por encima de 2000
        if other >= 2001:
            return 5, "Iron"

    async def _resolve_channel(
        self, channel_id: int, *, fallback: discord.abc.Messageable
    ) -> discord.abc.Messageable:
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
        name="reroll", description="Vote to reroll the songs in the current room."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def reroll(self, interaction: discord.Interaction):
        # Debe ejecutarse dentro de un thread de room
        ch = interaction.channel
        if not isinstance(ch, (discord.Thread, discord.TextChannel)):
            await interaction.response.send_message(
                "This command must be used inside a room thread.", ephemeral=True
            )
            return

        # Encontrar room por thread
        room_id = None
        room = None

        for rid, info in (self.rooms or {}).items():
            thr_id = info.get("thread")

            if thr_id is not None and thr_id == ch.id:
                room_id = rid
                room = info
                break

        if room is None:
            await interaction.response.send_message(
                "This does not look like an active room thread.", ephemeral=True
            )
            return

        if room.get("finished") or room.get("closed"):
            await interaction.response.send_message(
                "This room is already closed.", ephemeral=True
            )
            return

        if room.get("rerolled"):
            await interaction.response.send_message(
                "You can only reroll the songs once.", ephemeral=True
            )
            return

        # Resolver lista actual de jugadores (Members)
        raw_players = room.get("players", []) or []
        players_now: list[discord.Member] = []
        for p in raw_players:
            if isinstance(p, discord.Member):
                if not p.bot:
                    players_now.append(p)
            elif isinstance(p, discord.User):
                # best effort: convertir a Member si está en guild
                if interaction.guild:
                    m = interaction.guild.get_member(p.id)
                    if m and not m.bot:
                        players_now.append(m)
            elif isinstance(p, int):
                if interaction.guild:
                    m = interaction.guild.get_member(p)
                    if m and not m.bot:
                        players_now.append(m)

        # Si no hay suficientes miembros, igual dejamos votar pero con umbral mínimo 1
        n_players = len(players_now)
        threshold = max(1, (n_players // 2) + 1)  # mayoría simple

        # Estructura de votos
        votes = room.setdefault("reroll_votes", {})
        uid = interaction.user.id

        # Registrar voto "YES"
        votes[str(uid)] = True

        yes_count = sum(1 for v in votes.values() if v is True)

        # Si no alcanza, solo reporta estado
        if yes_count < threshold:
            try:
                await interaction.response.send_message(
                    f"🗳️ Reroll vote registered. YES: {yes_count}/{threshold} required.",
                    ephemeral=False,
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    f"🗳️ Reroll vote registered. YES: {yes_count}/{threshold} required."
                )
            return

        # Aprobado: cerrar room
        room["rerolled"] = True

        # Notificar en el thread
        try:
            await ch.send("🔄 Song reroll approved by vote.")
        except Exception:
            pass

        room["rerolling"] = True

        # Disparar evento para Rooms cog
        try:
            self.bot.dispatch("room_updated", room_id)
        except Exception:
            pass

        await self.launch_song_poll(
            room
        )  # esto postea "Selected songs..." en el thread

        # Responder interacción
        try:
            await interaction.response.send_message(
                "✅ Reroll confirmed.", ephemeral=True
            )
        except discord.InteractionResponded:
            await interaction.followup.send("✅ Reroll confirmed.", ephemeral=True)

    @app_commands.command(
        name="forfeit", description="Vote to forfeit the current room."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def forfeit(self, interaction: discord.Interaction):
        # Debe ejecutarse dentro de un thread de room
        ch = interaction.channel
        if not isinstance(ch, (discord.Thread, discord.TextChannel)):
            await interaction.response.send_message(
                "This command must be used inside a room thread.", ephemeral=True
            )
            return

        # Encontrar room por thread
        room_id = None
        room = None

        for rid, info in (self.rooms or {}).items():
            thr_id = info.get("thread")

            if thr_id is not None and thr_id == ch.id:
                room_id = rid
                room = info
                break

        if room is None:
            await interaction.response.send_message(
                "This does not look like an active room thread.", ephemeral=True
            )
            return

        if room.get("finished") or room.get("closed"):
            await interaction.response.send_message(
                "This room is already closed.", ephemeral=True
            )
            return

        # Resolver lista actual de jugadores (Members)
        raw_players = room.get("players", []) or []
        players_now: list[discord.Member] = []
        for p in raw_players:
            if isinstance(p, discord.Member):
                if not p.bot:
                    players_now.append(p)
            elif isinstance(p, discord.User):
                # best effort: convertir a Member si está en guild
                if interaction.guild:
                    m = interaction.guild.get_member(p.id)
                    if m and not m.bot:
                        players_now.append(m)
            elif isinstance(p, int):
                if interaction.guild:
                    m = interaction.guild.get_member(p)
                    if m and not m.bot:
                        players_now.append(m)

        # Si no hay suficientes miembros, igual dejamos votar pero con umbral mínimo 1
        n_players = len(players_now)
        threshold = max(1, (n_players // 2) + 1)  # mayoría simple

        # Estructura de votos
        votes = room.setdefault("forfeit_votes", {})
        uid = interaction.user.id

        # Registrar voto "YES"
        votes[str(uid)] = True

        yes_count = sum(1 for v in votes.values() if v is True)

        # Si no alcanza, solo reporta estado
        if yes_count < threshold:
            try:
                await interaction.response.send_message(
                    f"🗳️ Forfeit vote registered. YES: {yes_count}/{threshold} required.",
                    ephemeral=False,
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    f"🗳️ Forfeit vote registered. YES: {yes_count}/{threshold} required."
                )
            return

        # Aprobado: cerrar room
        room["finished"] = True
        room["closed"] = True

        # ✅ PUNTO 5: remover roles de queue a quienes siguen en la room
        try:
            await self._remove_queue_roles(players_now)
        except Exception:
            pass

        # Notificar en el thread
        try:
            if isinstance(ch, discord.Thread):
                await ch.send("🏳️ Forfeit approved by vote. Closing room.")
            else:
                await ch.send("🏳️ Forfeit approved by vote. Closing room.")
        except Exception:
            pass

        # Disparar evento para Rooms cog
        try:
            self.bot.dispatch("room_finished", room_id)
        except Exception:
            pass

        # Cerrar hilo
        try:
            # Si tu close_room ya maneja delete/archive según flags
            asyncio.create_task(self.close_room(ch, delay=5, delete=True))
        except Exception:
            pass

        # Responder interacción
        try:
            await interaction.response.send_message(
                "✅ Forfeit confirmed. Room will be closed.", ephemeral=True
            )
        except discord.InteractionResponded:
            await interaction.followup.send(
                "✅ Forfeit confirmed. Room will be closed.", ephemeral=True
            )

    @commands.command(name="calc", aliases=["calculate"])
    async def calc(self, ctx: commands.Context):
        room = next(
            (
                r
                for r in self.rooms.values()
                if r.get("thread") and r["thread"] == ctx.channel.id
            ),
            None,
        )
        if not room:
            return await ctx.send("Only works inside a room thread")

        # ⛔ sala ya finalizada
        if room.get("finished", False):
            return await ctx.send("this room has been submited results")

        # ⛔ votación en curso
        if room.get("vote_in_progress", False):
            return await ctx.send(
                "There is already a submit vote in progress in this room."
            )

        # Deben haberse seleccionado canciones (started=True)
        if room.get("started") is False:
            return await ctx.send(
                "Songs have not been selected yet. "
                "The room starts on the scheduled slot when it reaches 5 players."
            )

        lines = ctx.message.content.splitlines()

        # remove the command itself (!calc)
        lines = lines[1:]

        if len(lines) != 5:
            await ctx.send("Format:\n!calc\n(row x5))")
            return

        greats = goods = bads = misses = 0

        for row in lines:
            g, gd, b, m = map(int, row.strip().split())
            greats += g
            goods += gd
            bads += b
            misses += m

        perfects = room.get("total_notes") - (greats + goods + bads + misses)

        await ctx.reply(f"{perfects},{greats},{goods},{bads},{misses}")

    @commands.command(name="submit")
    async def submit(self, ctx: commands.Context, *, block: str):
        room = next(
            (
                r
                for r in self.rooms.values()
                if r.get("thread") and r["thread"] == ctx.channel.id
            ),
            None,
        )
        if not room:
            return await ctx.send("Only works inside a room thread")

        # ⛔ sala ya finalizada
        if room.get("finished", False):
            return await ctx.send("this room has been submited results")

        # ⛔ votación en curso
        if room.get("vote_in_progress", False):
            return await ctx.send(
                "There is already a submit vote in progress in this room."
            )

        # Deben haberse seleccionado canciones (started=True)
        if room.get("started") is False:
            return await ctx.send(
                "Songs have not been selected yet. "
                "The room starts on the scheduled slot when it reaches 5 players."
            )

        players: list[discord.Member] = [
            m for m in room.get("players", []) if m and not m.bot
        ]
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
            cc = mobj.group("cc")
            ids_in_lines.append(uid)
            by_id[uid] = (cc, stats)

        # Duplicados + miembros coinciden con sala
        if len(ids_in_lines) != len(set(ids_in_lines)):
            return await ctx.send("❌ Duplicate users in submit.")

        ids_set = set(ids_in_lines)
        missing = players_set - ids_set
        extra = ids_set - players_set
        if missing or extra:
            msg = []
            if missing:
                msg.append(
                    "Missing from submit: " + ", ".join(f"<@{x}>" for x in missing)
                )
            if extra:
                msg.append("Not in the room: " + ", ".join(f"<@{x}>" for x in extra))
            return await ctx.send("❌ Users do not match the room.\n" + "\n".join(msg))

        # Bloquear submits mientras dura la votación
        room["vote_in_progress"] = True

        try:
            # ---- Votación ----
            intro = (
                f"✅ React ✅ to validate, ❎ to decline (auto-close in {VOTE_TIMEOUT_SUBMIT}s)\n"
                "```\n" + "\n".join(lines) + "\n```"
            )
            vote_msg = await ctx.send(intro)
            for emo in ("✅", "❎"):
                try:
                    await vote_msg.add_reaction(emo)
                except Exception:
                    pass

            allowed_voters = {m.id for m in players}
            total_voters = len(allowed_voters)

            counts = {"✅": 0, "❎": 0}
            voted = set()
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
                    reaction, user = await self.bot.wait_for(
                        "reaction_add", timeout=remaining, check=check
                    )
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
                else:
                    ganador = "❎"

            try:
                await vote_msg.edit(
                    content=f"🗳️ Voting closed → {ganador}  (✅ {counts['✅']} / ❎ {counts['❎']})"
                )
            except Exception:
                pass

            if ganador != "✅":
                return await ctx.send(
                    "Submit declined. There might be an error, try doing it again."
                )

            # ==============================
            # VOTO APROBADO → aplicar MMR
            # ==============================
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}

            result_id = await self._next_result_id()

            # players_list UNA sola vez
            players_list = []
            for member in players:
                uid = member.id
                cc, stats = by_id[uid]
                old, current_role = await self.fetch_player(uid)

                profile_cc = (
                    await self.db_pool.fetchval(
                        "SELECT country FROM players WHERE user_id = $1",
                        uid,
                    )
                    or ""
                )

                if not profile_cc and cc:
                    profile_cc = cc.upper()
                    await self.db_pool.execute(
                        "UPDATE players SET country = $2 WHERE user_id = $1",
                        uid,
                        profile_cc,
                    )

                display_flag = country_flag(profile_cc)

                total_points = sum(s * w for s, w in zip(stats, [4, 3, 2, 1, 0]))
                players_list.append(
                    {
                        "member": member,
                        "cc": profile_cc,
                        "stats": stats,
                        "total": total_points,
                        "old": int(old),
                        "mmr_actual": int(old),
                        "role": current_role,
                        "display_flag": display_flag,
                        "was_placement": (current_role == "Placement"),
                        "seed_mmr": None,
                        "seed_rank": None,
                    }
                )

            # Seed Placement
            for p in players_list:
                if p["role"] == "Placement":
                    seed_mmr, seed_rank = self.placement_seed_from_stats(p["stats"])
                    p["mmr_actual"] = int(seed_mmr)
                    p["seed_mmr"] = int(seed_mmr)
                    p["seed_rank"] = seed_rank
                    p["was_placement"] = True

            # Room MMR (con seeds)
            room_mmr = (
                round(
                    sum(int(p["mmr_actual"]) for p in players_list) / len(players_list)
                )
                if players_list
                else None
            )

            # Orden final: puntos desc; empate → Placement primero
            def sort_key(p):
                return (p["total"], 1 if p["was_placement"] else 0)

            players_list.sort(key=sort_key, reverse=True)

            ratings = [int(p["mmr_actual"]) for p in players_list]
            order = list(range(len(players_list)))
            deltas = self.pairwise_deltas_simple(
                ratings, order, K=50, scale=400, cap=39
            )
            deltas = self._apply_tie_logic_placement_priority(players_list, deltas)

            season_row = await self.get_current_season()
            season_id = int(season_row["id"])
            season_name = season_row["name"]

            summary = []

            for idx, p in enumerate(players_list, start=1):
                mmr_prev = int(p["mmr_actual"])
                mmr_delta = int(deltas[idx - 1])
                mmr_final = mmr_prev + mmr_delta
                role_name = get_rank_from_mmr(mmr_final)

                stats = p["stats"]
                s_p, s_g, s_g2, s_b, s_m = stats

                # UPSERT seasonplayers
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

                # match_history
                await self.db_pool.execute(
                    """
                    INSERT INTO match_history (
                        season_id, result_id, user_id, pos,
                        s_perfect, s_great, s_good, s_bad, s_miss,
                        mmr_before, mmr_after, mmr_delta,
                        was_placement, is_win
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
                    bool(p["was_placement"]),
                    mmr_delta >= 0,
                )

                # Roles de rank
                role_id = RANK_ROLE_IDS.get(role_name)
                if role_id:
                    role_obj = ctx.guild.get_role(role_id)
                    if role_obj:
                        old_ranks = set(RANK_ROLE_IDS.values()) | {PLACEMENT_ROLE_ID}
                        keep_roles = [
                            r for r in p["member"].roles if r.id not in old_ranks
                        ]
                        new_roles = list(dict.fromkeys(keep_roles + [role_obj]))
                        try:
                            await p["member"].edit(
                                roles=new_roles, reason="Rank update"
                            )
                        except (discord.Forbidden, discord.HTTPException) as e:
                            await ctx.send(
                                f"⚠️ No pude actualizar roles de {p['member'].mention}: {e}"
                            )

                # Nick opcional
                if ctx.guild.me.guild_permissions.manage_nicknames:
                    base = p["member"].display_name
                    nick = f"{base} [{role_name}]"
                    if len(nick) > 32:
                        nick = f"{base[:max(0, 32 - len(role_name) - 3)]} [{role_name}]"
                    try:
                        await p["member"].edit(nick=nick)
                    except (discord.Forbidden, discord.HTTPException) as e:
                        await ctx.send(
                            f"⚠️ No pude cambiar nick de {p['member'].mention}: {e}"
                        )

                name_with_flag = (
                    f"{p['member'].display_name} {p['display_flag']}".rstrip()
                )
                summary.append(
                    (
                        idx,
                        name_with_flag,
                        p["total"],
                        p["stats"],
                        mmr_prev,
                        mmr_delta,
                        mmr_final,
                        p["was_placement"],
                        get_rank_from_mmr(mmr_prev),
                    )
                )

            # PUBLICAR RESULTADOS
            join_parent = ctx.channel.parent
            result_chan_id = (
                JOIN_TO_RESULTS.get(join_parent.id) if join_parent else None
            )
            result_chan = (
                await self._resolve_channel(result_chan_id, fallback=ctx.channel)
                if result_chan_id
                else ctx.channel
            )

            gap_low = room.get("gap_low")
            gap_high = room.get("gap_high")
            songs = room.get("songs") or []

            if songs:
                await self.db_pool.executemany(
                    """
                    INSERT INTO match_songs (result_id, song_name, level, difficulty, notes_count)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    [
                        (result_id, title, lvl, diff, notes)
                        for (title, lvl, diff, notes) in songs
                    ],
                )

            gap_text = ""
            if gap_low is not None and gap_high is not None:
                gap_text = f" · Lv {gap_low}-{gap_high}"
                if room_mmr is not None:
                    gap_text += f" · Avg MMR {room_mmr}"

            songs_block = ""
            if songs:
                song_lines = [
                    f"Lv {lvl} {diff} — {title} ({notes} notes)"
                    for (title, lvl, diff, notes) in songs
                ]
                songs_block = "\n**Songs (Lv / Diff / Notes):**\n" + "\n".join(
                    song_lines
                )

            header_line = f"ID: {result_id}{gap_text}{songs_block}"

            buf = generate_results_image(
                summary,
                font_path="assets/rodinntlg_eb.otf",
                side_image=Image.open("assets/logo.png"),
            )
            image_bytes = buf.read()

            embed = discord.Embed(
                title=f"RESULTS — {season_name}",
                description=header_line,
                color=0x00F7FF,
            ).set_image(url="attachment://results.png")

            msg_result = await result_chan.send(
                embed=embed, file=discord.File(io.BytesIO(image_bytes), "results.png")
            )
            await ctx.send(
                embed=embed, file=discord.File(io.BytesIO(image_bytes), "results.png")
            )
            await result_chan.send("MMR Updated")
            await ctx.send("MMR Updated")

            await self.db_pool.execute(
                """
                INSERT INTO results (
                    result_id, season_id, season_name, channel_id, message_id,
                    gap_low, gap_high, room_mmr, songs_block
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                result_id,
                season_id,
                season_name,
                msg_result.channel.id,
                msg_result.id,
                gap_low,
                gap_high,
                room_mmr,
                songs_block.strip() if songs_block else None,
            )

            # ✅ Quitar roles queue a los jugadores del match (EN/JP)
            try:
                await self._remove_queue_roles(players)
            except Exception:
                pass

            room["closed"] = True
            room["finished"] = True

            await ctx.send("This room has been submited results")

            rid = next(
                (
                    rid
                    for rid, r in self.rooms.items()
                    if r.get("thread") and r["thread"] == ctx.channel.id
                ),
                None,
            )
            if rid is not None:
                try:
                    self.bot.dispatch("room_finished", rid)
                except Exception:
                    pass

            asyncio.create_task(self.close_room(ctx.channel, delay=60, delete=True))

        finally:
            room["vote_in_progress"] = False

    async def _apply_rank_role(
        self, guild: discord.Guild, member_or_id, role_name: str, reason: str = ""
    ) -> bool:
        rank_role_ids = set(RANK_ROLE_IDS.values())

        # Resolver member
        member = (
            member_or_id
            if isinstance(member_or_id, discord.Member)
            else guild.get_member(int(member_or_id))
        )
        if member is None:
            # Como fallback, intenta fetch_member (puede fallar si no está en el guild)
            try:
                member = await guild.fetch_member(int(member_or_id))
            except Exception:
                return False

        # Normaliza y resuelve rol objetivo (fallback a Placement)
        role_name = (role_name or "Placement").strip()
        if role_name not in RANK_ROLE_IDS:
            role_name = "Placement"

        target_role = guild.get_role(RANK_ROLE_IDS[role_name])
        if not target_role:
            # Si el rol no existe en el servidor, cae a Placement si existe; si no, no se puede asignar.
            target_role = guild.get_role(RANK_ROLE_IDS["Placement"])
            if not target_role:
                return False

        # Mantener roles NO-rango y agregar el nuevo rango
        keep = [r for r in member.roles if r.id not in rank_role_ids]
        new_roles = list(dict.fromkeys(keep + [target_role]))

        try:
            await member.edit(
                roles=new_roles, reason=reason or f"Apply rank {role_name}"
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    @commands.command(name="edit_user_mmr")
    async def edit_user_mmr(
        self, ctx: commands.Context, member: discord.Member, new_value: int
    ):
        if ctx.author.id not in [878310498720940102, 1364401080661708892]:
            return await ctx.reply("❌ No tienes permiso para ejecutar este comando.")
        try:
            season_row = await self.get_current_season()
            season_id = int(season_row["id"])
            role_name = get_rank_from_mmr(new_value)
            await self.db_pool.execute(
                """
                INSERT INTO seasonplayers (season_id, user_id, mmr, role)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (season_id, user_id)
                DO UPDATE SET mmr = EXCLUDED.mmr, role = EXCLUDED.role
                """,
                season_id,
                member.id,
                new_value,
                role_name,
            )
            await self._apply_rank_role(
                ctx.guild, member, role_name, reason="Applying user's role (mmr edit)"
            )
            await ctx.reply("Done!")
        except Exception as e:
            await ctx.reply(str(e))

    @commands.command(name="fix_user_rank_roles")
    async def fix_user_rank_roles(self, ctx: commands.Context, member: discord.Member):
        if ctx.author.id not in [878310498720940102, 1364401080661708892]:
            return await ctx.reply("❌ No tienes permiso para ejecutar este comando.")
        try:
            result = await self.db_pool.fetchrow(
                """
                SELECT role FROM seasonplayers WHERE user_id = $1;
                """,
                member.id,
            )
            result = dict(result)
            await self._apply_rank_role(
                ctx.guild, member, result["role"], reason="Fixing user's ranked roles"
            )
            await ctx.reply("Done!")
        except Exception as e:
            await ctx.reply(str(e))


async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))



