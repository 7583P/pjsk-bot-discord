import os, io, re, json, secrets
import random
import asyncio
import datetime
import aiohttp  # descarga catálogo canciones
import asyncpg  # PostgreSQL asíncrono
import discord
import logging
import inspect
import datetime as dt
from discord import app_commands, Embed
from discord import ui, ButtonStyle
from discord import Thread, TextChannel, MessageType
from discord.ext import commands, tasks  # loops periódicos
from discord.ui import View, button
from zoneinfo import ZoneInfo

from helpers.image import generate_results_image, generate_stats_image
from PIL import Image, ImageDraw, ImageFont


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
    "jp": 1451259854693072927,
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

# =========================
# INFRA / STABILITY SETTINGS
# =========================
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "10"))
DB_COMMAND_TIMEOUT = float(os.getenv("DB_COMMAND_TIMEOUT", "30"))

DISCORD_IO_CONCURRENCY = int(os.getenv("DISCORD_IO_CONCURRENCY", "5"))
SONG_POLL_TIMEOUT_SEC = float(os.getenv("SONG_POLL_TIMEOUT_SEC", "25"))
FORFEIT_TIMEOUT_SEC = float(os.getenv("FORFEIT_TIMEOUT_SEC", "30"))

VALID_SONG_SERVERS = {"all", "en", "jp"}
VALID_SONG_DIFFS = {"easy", "normal", "hard", "expert", "master", "append"}

# Canciones baneadas por defecto de la queue.
# Se filtran por title_key robusto y también queda soporte para song_id.
DEFAULT_BANNED_QUEUE_SONGS = (
    "What Sort of Ending Are You Wishing For?",
    "A Sad Farewell at the Curtain Call",
)

ACCURACY_CARD_REMINDER = (
    "Reminder: don't have an accuracy card on your lead card. "
    "You can use a 1☆ card if you don't want to search."
)

ENTRY_RE = re.compile(
    r"^<@!?(?P<id>\d+)>\s*(?:\[(?P<cc>[A-Za-z]{2}|\?\?|--)\])?\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$"
)


def country_flag(code: str) -> str:
    code = code.upper()
    if len(code) != 2:
        return ""
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


UNKNOWN_FLAG_CODE = "__UNKNOWN_FLAG__"
UNKNOWN_FLAG_IMAGE_PATH = "assets/flags/unknown.jpg"

UNKNOWN_COUNTRY_CODES = {
    "",
    "WW",
    "XX",
    "ZZ",
    "UN",
    "??",
    "--",
    "NONE",
    "NULL",
    "UNKNOWN",
    UNKNOWN_FLAG_CODE,
}

ISO2_CODE_RE = re.compile(r"^[A-Za-z]{2}$")


def is_unknown_country_code(code: str | None) -> bool:
    return (code or "").upper().strip() in UNKNOWN_COUNTRY_CODES


def normalize_submit_country_code(code: str | None) -> str:
    cc = (code or "").upper().strip()

    if is_unknown_country_code(cc):
        return ""

    if not ISO2_CODE_RE.match(cc):
        return ""

    return cc


def country_code_for_results(stored_code: str | None, submit_code: str | None) -> str:
    """
    Devuelve el código que se usará SOLO para dibujar results.png.
    No modifica players.country en PostgreSQL.
    Prioridad:
    1) players.country guardado en DB.
    2) [CC] enviado opcionalmente en !submit.
    3) assets/flags/unknown.jpg.
    """
    stored_cc = normalize_submit_country_code(stored_code)
    if stored_cc:
        return stored_cc

    submit_cc = normalize_submit_country_code(submit_code)
    if submit_cc:
        return submit_cc

    return UNKNOWN_FLAG_CODE


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
SEASON_PLACEMENT_ROLE_ID = 1470417525635551314  # <-- el rol nuevo de season reset
SEASON_RESET_PENALTY = 50


SEASON_CHOICES = [
    app_commands.Choice(name="Current", value="current"),
    app_commands.Choice(name="Season 3", value="season 3"),
    app_commands.Choice(name="Season 2", value="season 2"),
    app_commands.Choice(name="Season 1", value="season 1"),
    app_commands.Choice(name="Pre Season", value="pre season"),
    app_commands.Choice(name="Beta Test Season", value="beta test season"),
    app_commands.Choice(name="Alpha Test Season", value="alpha test season"),
]

# Solo para /mmr_user: permite ver una season concreta o el historial completo.
MMR_USER_SEASON_CHOICES = [
    app_commands.Choice(name="All Seasons", value="all"),
    *SEASON_CHOICES,
]

class Top100View(View):
    def __init__(self, cog, db_pool, interaction, season_id, season_name):
        super().__init__(timeout=180)
        self.cog = cog
        self.db = db_pool
        self.inter = interaction
        self.season_id = season_id
        self.season_name = season_name
        self.page = 0
        self.per_page = 20
        self._rows_cache: list[dict] | None = None
        self._cache_lock = asyncio.Lock()

    async def _get_rows(self, force_refresh: bool = False) -> list[dict]:
        """
        Cachea el leaderboard para que /top100 y los botones no vuelvan a consultar
        Discord/DB en cada página. Esto evita 429 y Unknown interaction.
        """
        async with self._cache_lock:
            if force_refresh or self._rows_cache is None:
                self._rows_cache = await self.cog._build_visible_leaderboard_rows(
                    self.inter.guild,
                    self.season_id,
                )
            return self._rows_cache

    async def build_embed(self) -> Embed:
        all_rows = await self._get_rows()

        total = min(len(all_rows), 100)
        max_page = (total - 1) // self.per_page if total else 0
        self.page = max(0, min(self.page, max_page))

        offset = self.page * self.per_page
        rows = all_rows[:100][offset : offset + self.per_page]

        embed = Embed(title=f"PJSK LOUNGE TOP 100 — {self.season_name}", color=0x00FFFB)
        header = "TOP| Name                     |   MMR   | Rank\n"
        separator = "---+--------------------------+---------+----------------\n"
        lines = []

        for idx, r in enumerate(rows, start=offset + 1):
            name = str(r.get("member_name") or f"ID {r['user_id']}")[:25]
            mmr = f"{int(r['visible_mmr']):,}"
            rank_label = f"{r['role']} [SP]" if r["is_unplayed_sp"] else r["role"]

            lines.append(f"{idx:>2} | {name:<25} | {mmr:>7} | {rank_label}")

        embed.description = (
            "```\n"
            + header
            + separator
            + ("\n".join(lines) if lines else "(no players)\n")
            + f"\n```Página {self.page + 1}/{max_page + 1}"
        )
        return embed

    @button(emoji="◀️", style=ButtonStyle.secondary)
    async def prev_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()

        if self.page > 0:
            self.page -= 1

        total = min(len(await self._get_rows()), 100)
        max_page = (total - 1) // self.per_page if total else 0

        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page == max_page

        new_embed = await self.build_embed()
        await interaction.edit_original_response(embed=new_embed, view=self)

    @button(emoji="▶️", style=ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()

        total = min(len(await self._get_rows()), 100)
        max_page = (total - 1) // self.per_page if total else 0

        if self.page < max_page:
            self.page += 1

        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page == max_page

        new_embed = await self.build_embed()
        await interaction.edit_original_response(embed=new_embed, view=self)

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
        
    def _season_status_label(self, member: discord.Member | None) -> str | None:
        if member and self._has_season_placement_role(member):
            return "Season Placement"
        return None


    async def _is_current_season_id(self, season_id: int | None) -> bool:
        if not season_id:
            return False
        current = await self.get_current_season()
        return bool(current and int(current["id"]) == int(season_id))

    async def _display_mmr_for_member(
        self,
        member: discord.Member | None,
        real_mmr: int,
        season_id: int | None,
    ) -> int:
        """
        Devuelve el MMR visible.
        Season Placement ya no se fuerza a 0: se muestra su Starting MMR real.
        """
        return int(real_mmr)
    
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

    def _has_season_placement_role(self, member: discord.Member) -> bool:
        return any(r.id == SEASON_PLACEMENT_ROLE_ID for r in getattr(member, "roles", []))

    def _is_legacy_placement_for_sort(self, member: discord.Member | None) -> bool:
        """
        Placement normal debe quedar por debajo de cualquier MMR numérico,
        incluso por debajo de 0 MMR.

        Season Placement NO entra aquí: se ordena por su MMR visible.
        """
        if member is None:
            return False
        return bool(
            self._is_placement_member(member)
            and not self._has_season_placement_role(member)
        )

    def _queue_member_sort_key(
        self,
        member: discord.Member,
        mmr_map: dict[int, int],
        join_order: dict[int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        """
        Orden global para queue/future rooms:
        1) Jugadores con MMR numérico, de mayor a menor.
        2) Empate: orden real de entrada a la queue.
        3) Placement normal al final, debajo de 0 MMR.
        """
        join_order = join_order or {}
        joined_at = int(join_order.get(member.id, 10**18))

        if self._is_legacy_placement_for_sort(member):
            return (1, 0, joined_at, int(member.id))

        return (0, -int(mmr_map.get(member.id, 0)), joined_at, int(member.id))

    @staticmethod
    def _room_row_sort_key(row: dict) -> tuple[int, int, str, int]:
        """
        Orden visual dentro de un room board:
        Placement normal siempre debajo de cualquier MMR numérico, incluido 0.
        """
        status = str(row.get("status") or "")
        is_legacy_placement = 1 if status == "placement" else 0
        mmr = int(row.get("mmr") or 0)
        name = str(row.get("name") or "").lower()
        uid = int(row.get("user_id") or 0)
        return (is_legacy_placement, -mmr, name, uid)

    @staticmethod
    def _clamp_mmr(value: int) -> int:
        return max(0, int(value))
    

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

    @staticmethod
    def _overlay_submitted_at_text_on_results_image(
        buf: io.BytesIO,
        submitted_at_text: str,
        font_path: str = "assets/rodinntlg_eb.otf",
    ) -> io.BytesIO:
        """
        Fallback visual: si helpers.image.generate_results_image todavía no soporta
        submitted_at_text, dibuja la fecha/hora en la imagen sin romper el submit.
        """
        try:
            buf.seek(0)
            img = Image.open(buf).convert("RGBA")
            draw = ImageDraw.Draw(img)
            w, h = img.size

            font_size = max(24, int(h * 0.032))
            try:
                font = ImageFont.truetype(font_path, font_size)
            except Exception:
                font = ImageFont.load_default()

            text = f"SUBMITTED: {submitted_at_text}"
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = max(0, (w - tw) // 2)
            y = max(70, int(h * 0.115))

            # sombra suave para que se lea sobre fondos claros u oscuros
            draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 190))
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

            out = io.BytesIO()
            img.convert("RGB").save(out, format="PNG")
            out.seek(0)
            return out
        except Exception:
            buf.seek(0)
            return buf

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

    def _lock_unlock_dt_for_current_prep(self, now: dt.datetime) -> dt.datetime | None:
        prep_start = self._in_prep_window(now)
        if prep_start is None:
            return None
        return self._slot_start_from_prep_start(now, prep_start)

    def _format_mmss_until(self, target: dt.datetime, now: dt.datetime) -> str:
        remaining = max(0, int((target - now).total_seconds()))
        minutes = remaining // 60
        seconds = remaining % 60
        return f"{minutes}:{seconds:02d}"

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

    @staticmethod
    def _room_is_locked_for_player_exit(info: dict | None) -> bool:
        """
        True cuando el jugador ya no puede salirse voluntariamente de la room.

        Lockdown empieza cuando el bot crea el hilo/room y manda
        "You are locked in for this slot". Desde ese momento:
        - /d ya no puede remover jugadores del room.
        - Si alguien sale del server, sigue contando hasta submit.
        - El submit debe seguir esperando las 5 líneas.
        """
        if not info or info.get("finished", False):
            return False

        phase = info.get("phase")
        return bool(
            info.get("locked_in_thread", False)
            or info.get("slot_start_ts") is not None
            or info.get("started", False)
            or phase in (PHASE_FUTURE, PHASE_PREPARED, PHASE_STARTED)
        )


    def _current_prep_slot_ts(self, now: dt.datetime) -> int | None:
        """
        Si estamos en PREP (:25-:29 o :55-:59), devuelve el timestamp
        del slot que se está preparando.
        Ejemplo: :25-:29 -> :30, :55-:59 -> siguiente :00.
        """
        prep_start = self._in_prep_window(now)
        if prep_start is None:
            return None

        slot_dt = self._slot_start_from_prep_start(now, prep_start)
        return int(slot_dt.timestamp())

    def _count_locked_rooms_for_slot(self, join_id: int, slot_ts: int) -> int:
        """
        Cuenta rooms ya creadas/locked para un join channel y slot específico.
        Sirve para bloquear /c durante los 5 minutos PREP cuando la queue
        ya quedó exacta en salas completas de 5.
        """
        count = 0
        for _rid, info in self.rooms.items():
            if info.get("finished", False):
                continue
            if int(info.get("join_channel_id") or 0) != int(join_id):
                continue
            if int(info.get("slot_start_ts") or 0) != int(slot_ts):
                continue

            phase = info.get("phase")
            if phase in (PHASE_FUTURE, PHASE_PREPARED, PHASE_STARTED):
                count += 1
                continue
            if bool(info.get("locked_in_thread", False)):
                count += 1

        return count


    async def _effective_display_mmrs(self, members: list[discord.Member]) -> dict[int, int]:
        """
        Devuelve el MMR que debe mostrarse en Discord:
        - Si tiene Season Placement -> (prev_season_mmr - 50), clamp >= 0
        - Si no -> current season mmr
        """
        if not members:
            return {}

        season_row = await self.get_current_season()
        if not season_row:
            return {m.id: 0 for m in members}

        cur_id = int(season_row["id"])
        prev_row = await self._get_previous_season(cur_id)
        prev_id = int(prev_row["id"]) if prev_row else None

        ids = [m.id for m in members]

        # current season mmr (base)
        current_map = await self._fetch_mmrs_batch(ids)

        # override para Season Placement
        sp_members = [m for m in members if self._has_season_placement_role(m)]
        if prev_id and sp_members:
            prev_map = await self._fetch_mmrs_batch_for_season(prev_id, [m.id for m in sp_members])
            for m in sp_members:
                seeded = self._clamp_mmr(int(prev_map.get(m.id, 0)) - SEASON_RESET_PENALTY)
                current_map[m.id] = seeded

        return current_map

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
        slot_ts = int(slot_dt.timestamp())
        ordered = await self._get_ordered_queue_members(guild, join_id)
        qlen = len(ordered)
        locked_rooms = self._count_locked_rooms_for_slot(join_id, slot_ts)

        # Si ya se crearon rooms exactas para este slot, no se permite nueva queue.
        if locked_rooms > 0 and qlen == 0:
            try:
                mmss = self._format_mmss_until(slot_dt, now)
                await join_channel.send(
                    f"**{mins_left} min left** — Queue starting at <t:{slot_ts}:T> (<t:{slot_ts}:R>). "
                    f"✅ Room queue for this slot is full. Queue is locked until <t:{slot_ts}:T> "
                    f"— **{mmss}** left."
                )
            except Exception:
                pass
            return

        # Need se calcula sobre la queue completa que todavía NO fue convertida a rooms.
        # Ej: 6 => falta 4 para la segunda room; 9 => falta 1; 10 => completo.
        if qlen == 0:
            need = ROOM_CAPACITY
        else:
            rem = qlen % ROOM_CAPACITY
            need = 0 if rem == 0 else (ROOM_CAPACITY - rem)

        ping_role_id = PING_ROLES_BY_JOIN_CHANNEL.get(join_id)
        do_ping = (
            ping_role_id is not None
            and mins_left == 5
            and qlen > 0
            and need > 0
        )
        ping_part = f"<@&{ping_role_id}> " if do_ping else ""

        if qlen > 0 and need == 0:
            full_rooms = qlen // ROOM_CAPACITY
            plural = "s" if full_rooms != 1 else ""
            mmss = self._format_mmss_until(slot_dt, now)
            need_txt = f"✅ {full_rooms} room{plural} full. Queue is locked until <t:{slot_ts}:T> — **{mmss}** left."
        else:
            need_txt = f"Need {need} more player(s) to fill the next room."

        try:
            await join_channel.send(
                f"**{mins_left} min left** — Queue starting at <t:{slot_ts}:T> (<t:{slot_ts}:R>). "
                f"{ping_part}{need_txt} "
                f"Type `/c` to join the queue or `/d` to drop the queue.",
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=True,
                    everyone=False,
                ),
            )
        except Exception:
            pass


    async def _pend_unfilled_queue_on_slot(
        self, guild: discord.Guild, join_channel: discord.TextChannel, now: dt.datetime
    ):
        """
        Compatibilidad: no crea threads incompletos.
        La lógica final usa _prepare_rooms_for_join_channel() en START para crear
        rooms completas y _purge_unfilled_queue_on_slot() para dropear sobrantes.
        """
        return

    async def _purge_unfilled_queue_on_slot(
        self, guild: discord.Guild, join_channel: discord.TextChannel, now: dt.datetime
    ):
        """
        En el minuto exacto del slot (:00 / :30), elimina a los jugadores
        que quedaron en una room incompleta.

        Ejemplos:
        - 6 jugadores al slot: se crea room 1 con 5 y se dropea room 2 con 1.
        - 9 jugadores al slot: se crea room 1 con 5 y se dropea room 2 con 4.
        - 14 jugadores al slot: se crean rooms 1-2 y se dropea room 3 con 4.
        """
        join_id = join_channel.id
        ordered = await self._get_ordered_queue_members(guild, join_id)
        if not ordered:
            return

        rem = len(ordered) % ROOM_CAPACITY
        if rem == 0:
            return

        leftovers = ordered[-rem:]

        start_base = self._in_start_window(now)
        if start_base is not None:
            slot_dt = now.replace(second=0, microsecond=0, minute=start_base)
        else:
            slot_dt = now.replace(second=0, microsecond=0)
        slot_ts = int(slot_dt.timestamp())

        locked_rooms = self._count_locked_rooms_for_slot(join_id, slot_ts)
        dropped_room_no = locked_rooms + 1

        for m in leftovers:
            self.queues.get(join_id, set()).discard(m.id)
            self.queue_join_order.get(join_id, {}).pop(m.id, None)

        try:
            await self._remove_queue_roles(leftovers)
        except Exception:
            pass

        mentions = " ".join(m.mention for m in leftovers)
        try:
            await join_channel.send(
                f"⛔ Dropping players from room {dropped_room_no}: not enough players. {mentions}\n"
                f"Type `/c` to re-join for the next slot.",
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )
        except Exception:
            pass

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

        if not members:
            return []

        mmr_map = await self._effective_display_mmrs(members)
        join_order = self.queue_join_order.get(join_channel_id, {})

        members.sort(
            key=lambda m: self._queue_member_sort_key(m, mmr_map, join_order)
        )

        return members

    async def _is_unplayed_season_placement(
        self,
        member: discord.Member | None,
        season_id: int | None,
        matches_played: int = 0,
    ) -> bool:
        if member is None:
            return False

        return bool(
            matches_played == 0
            and await self._is_current_season_id(season_id)
            and self._has_season_placement_role(member)
        )


    async def _display_mmr_for_member(
        self,
        member: discord.Member | None,
        real_mmr: int,
        season_id: int | None,
        matches_played: int = 0,
    ) -> int:
        # Season Placement sin jugar debe mostrar su Starting MMR real, no 0.
        return int(real_mmr)


    async def _display_peak_mmr_for_member(
        self,
        member: discord.Member | None,
        real_peak_mmr: int,
        season_id: int | None,
        matches_played: int = 0,
    ) -> int:
        # Season Placement sin jugar debe mostrar su Starting MMR/Peak real, no 0.
        return int(real_peak_mmr)

    async def _mmr_label_for_member(
        self,
        member: discord.Member | None,
        real_mmr: int,
        season_id: int | None = None,
        matches_played: int = 0,
        *,
        with_unit: bool = False,
    ) -> str:
        """Texto único para rooms/#join/mmr.

        Season Placement debe seguir visible hasta que el submit confirme la partida.
        Por eso el label no se reduce a Starting MMR: también conserva el estado.
        """
        mmr = int(real_mmr or 0)

        if member is not None and self._has_season_placement_role(member):
            if with_unit:
                return f"{mmr} MMR · Season Placement"
            return f"{mmr} · Season Placement"

        if member is not None and self._is_placement_member(member):
            return "Placement"

        suffix = " MMR" if with_unit else ""
        return f"{mmr}{suffix}"


    async def _build_visible_leaderboard_rows(
        self,
        guild: discord.Guild,
        season_id: int,
    ) -> list[dict]:
        current = await self.get_current_season()
        is_current = bool(current and int(current["id"]) == int(season_id))

        prev_row = await self._get_previous_season(season_id) if is_current else None
        prev_id = int(prev_row["id"]) if prev_row else None

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    sp.user_id,
                    sp.mmr,
                    sp.role,
                    tie.first_reached_current_mmr,
                    COALESCE(mh.matches_played, 0) AS matches_played,
                    prev.prev_mmr,
                    prev.prev_pos
                FROM seasonplayers sp
                LEFT JOIN (
                    SELECT user_id, COUNT(*)::int AS matches_played
                    FROM match_history
                    WHERE season_id = $1
                    GROUP BY user_id
                ) mh
                    ON mh.user_id = sp.user_id
                LEFT JOIN (
                    SELECT
                        sp2.user_id,
                        MIN(mhist.played_at) AS first_reached_current_mmr
                    FROM seasonplayers sp2
                    LEFT JOIN match_history mhist
                      ON mhist.season_id = sp2.season_id
                     AND mhist.user_id = sp2.user_id
                     AND mhist.mmr_after = sp2.mmr
                    WHERE sp2.season_id = $1
                    GROUP BY sp2.user_id
                ) tie
                    ON tie.user_id = sp.user_id
                LEFT JOIN (
                    SELECT
                        sp2.user_id,
                        sp2.mmr AS prev_mmr,
                        RANK() OVER (ORDER BY sp2.mmr DESC, sp2.user_id ASC) AS prev_pos
                    FROM seasonplayers sp2
                    WHERE sp2.season_id = $2
                ) prev
                    ON prev.user_id = sp.user_id
                WHERE sp.season_id = $1
                """,
                season_id,
                prev_id,
            )

        out = []
        for r in rows:
            user_id = int(r["user_id"])

            # IMPORTANTE:
            # No usar guild.fetch_member() dentro del leaderboard.
            # En /top100 eran muchas llamadas REST a Discord y terminaban en 429 + Unknown interaction.
            member = guild.get_member(user_id) if guild else None
            member_name = member.display_name if member is not None else f"ID {user_id}"

            matches_played = int(r["matches_played"] or 0)
            mmr = int(r["mmr"] or 0)

            is_unplayed_sp = await self._is_unplayed_season_placement(
                member,
                season_id,
                matches_played,
            )

            out.append(
                {
                    "user_id": user_id,
                    "member": member,
                    "member_name": member_name,
                    "role": str(r["role"]),
                    "mmr": mmr,
                    "visible_mmr": mmr,
                    "display_mmr": mmr,
                    "starting_mmr": mmr if is_unplayed_sp else None,
                    "mmr_label": "Starting MMR" if is_unplayed_sp else "MMR",
                    "season_status": "Season Placement" if is_unplayed_sp else None,
                    "tie_reached_at": r["first_reached_current_mmr"],
                    "matches_played": matches_played,
                    "prev_mmr": int(r["prev_mmr"] or 0),
                    "prev_pos": int(r["prev_pos"] or 10**9),
                    "is_unplayed_sp": is_unplayed_sp,
                }
            )

        def sort_key(row: dict):
            # bucket 0 = jugadores que YA jugaron esta season
            # bucket 1 = Season Placement sin jugar
            # bucket 2 = resto sin jugar
            bucket = 0 if row["matches_played"] > 0 else (1 if row["is_unplayed_sp"] else 2)

            if bucket == 0:
                tie_at = row.get("tie_reached_at") or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
                return (0, -row["mmr"], tie_at, row["user_id"])

            if bucket == 1:
                # se ordenan por cómo quedaron en la season pasada
                return (1, row["prev_pos"], -row["prev_mmr"], row["user_id"])

            return (2, -row["mmr"], row["user_id"])

        out.sort(key=sort_key)
        return out

    async def _prepare_rooms_for_join_channel(
        self,
        guild: discord.Guild,
        join_channel: discord.TextChannel,
        *,
        slot_dt: dt.datetime | None = None,
        require_exact_multiple: bool = True,
    ):
        """
        Crea rooms completas de 5 jugadores.

        Regla durante PREP (:25-:29 / :55-:59):
        - Si la queue está exactamente en múltiplos de 5 (5, 10, 15...),
          crea todas esas rooms y deja el slot locked.
        - Si hay sobrantes (6-9, 11-14...), NO crea ninguna room todavía;
          se sigue pidiendo gente para completar la siguiente room.

        Regla al llegar START (:00 / :30):
        - Crea solo las rooms completas de 5.
        - Los sobrantes se dropean después en _purge_unfilled_queue_on_slot().
        """
        join_id = join_channel.id
        ordered = await self._get_ordered_queue_members(guild, join_id)
        qlen = len(ordered)

        if qlen < ROOM_CAPACITY:
            return

        if require_exact_multiple and (qlen % ROOM_CAPACITY != 0):
            return

        groups = [
            ordered[i : i + ROOM_CAPACITY]
            for i in range(0, len(ordered), ROOM_CAPACITY)
        ]
        full_groups = [g for g in groups if len(g) == ROOM_CAPACITY]
        if not full_groups:
            return

        now = self._now_lima()

        if slot_dt is None:
            prep_start = self._in_prep_window(now)
            if prep_start is None:
                return
            slot_dt = self._slot_start_from_prep_start(now, prep_start)
        else:
            slot_dt = slot_dt.replace(second=0, microsecond=0)

        slot_ts = int(slot_dt.timestamp())
        server_code = JOIN_CHANNEL_CFG.get(join_id, {}).get("server", "en")
        existing_slot_room_count = self._count_locked_rooms_for_slot(join_id, slot_ts)

        for i, group in enumerate(full_groups, start=existing_slot_room_count + 1):
            mmr_map = await self._effective_display_mmrs([m for m in group if not m.bot])
            mmr_vals = [int(mmr_map.get(m.id, 0)) for m in group]

            avg = (sum(mmr_vals) // len(mmr_vals)) if mmr_vals else 0
            rank = self._rank_label_from_mmr(avg)

            thread_name = self._build_thread_name(i, slot_dt, avg, rank)

            rid, info = await self._create_room(
                join_channel, server_code=server_code, thread_name=thread_name
            )

            for m in group:
                self.queues[join_id].discard(m.id)
                await self._add_member_to_room(m, rid, info)
                self.queue_join_order.get(join_id, {}).pop(m.id, None)

            info["phase"] = PHASE_FUTURE
            info["locked_in_thread"] = True
            info["slot_start_ts"] = slot_ts
            info["avg_mmr"] = avg
            info["rank_label"] = rank
            info["started"] = False
            info["songs_posted"] = False

            mentions = " ".join(m.mention for m in group)

            allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
            await self.bot.get_channel(info["join_channel_id"]).get_thread(
                info["thread"]
            ).send(
                f"{mentions}\n"
                f"✅ You are locked in for this slot.\n"
                f"🎵 Songs will be displayed at <t:{slot_ts}:T> (<t:{slot_ts}:R>).",
                allowed_mentions=allowed,
            )

            try:
                await self._remove_queue_roles(group)
            except Exception:
                pass

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

        await self._send_accuracy_card_reminder_once(
            info,
            started_ts=int(info.get("started_at") or 0),
        )

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

            try:
                parent_channel = self.bot.get_channel(info["join_channel_id"])
                if isinstance(parent_channel, discord.TextChannel):
                    await self._announce_room_start(parent_channel, rid, info)
            except Exception:
                pass

            await self._send_accuracy_card_reminder_once(
                info,
                started_ts=int(started_ts),
            )

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

    async def _fetch_country_codes_batch(self, user_ids: list[int]) -> dict[int, str]:
        """
        Lee players.country en batch para mostrar la bandera en results.png.
        Missing / vacío / WW / ?? / -- se tratan como unknown.jpg.
        """
        if not user_ids:
            return {}

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, COALESCE(country, '') AS country
                FROM players
                WHERE user_id = ANY($1::bigint[])
                """,
                user_ids,
            )

        out = {
            int(r["user_id"]): str(r["country"] or "").upper().strip()
            for r in rows
        }

        for uid in user_ids:
            out.setdefault(int(uid), "")

        return out

    async def _fetch_matches_played_batch(
        self,
        season_id: int,
        user_ids: list[int],
    ) -> dict[int, int]:
        """
        Devuelve cuántas partidas tenía cada usuario en match_history
        para esta season ANTES de insertar el submit actual.

        Uso visual: permite mostrar [PLACEMENT] solo cuando un usuario
        Placement normal está jugando su primera partida.
        """
        if not user_ids:
            return {}

        ids = [int(uid) for uid in user_ids]

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, COUNT(*)::int AS matches_played
                FROM match_history
                WHERE season_id = $1
                  AND user_id = ANY($2::bigint[])
                GROUP BY user_id
                """,
                int(season_id),
                ids,
            )

        out = {
            int(r["user_id"]): int(r["matches_played"] or 0)
            for r in rows
        }

        for uid in ids:
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

        mmr_map = await self._effective_display_mmrs(members_all)
        join_order = self.queue_join_order.get(parent_id, {})

        ordered = sorted(
            members_all,
            key=lambda m: self._queue_member_sort_key(m, mmr_map, join_order),
        )

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
        nonpl = [m for m in players if not self._is_placement_member(m)]
        mmr_map = await self._effective_display_mmrs(nonpl)
        lines = []
        for m in players:
            label = await self._mmr_label_for_member(m, int(mmr_map.get(m.id, 0)), with_unit=True)
            lines.append(f"- {m.display_name} ({label})")
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
        started_members = []
        for _rid, info in started_rooms:
            for m in info.get("players", []) or []:
                if m and not m.bot:
                    started_members.append(m)

        started_mmr_map = await self._effective_display_mmrs(started_members)

        lines: list[str] = []

        for rid, info in started_rooms:
            disp = self._disp(info, rid)
            started_at = info.get("started_at")
            ts_part = f"<t:{int(started_at)}:T>" if started_at else "—"

            lines.append(f"ROOM {disp} STARTED {ts_part}")

            players = [m for m in (info.get("players", []) or []) if m and not m.bot]
            rows = []
            for m in players:
                mmr = int(started_mmr_map.get(m.id, 0))
                if self._has_season_placement_role(m):
                    status = "season_placement"
                elif self._is_placement_member(m):
                    status = "placement"
                else:
                    status = "regular"

                rows.append({
                    "user_id": int(m.id),
                    "name": m.display_name,
                    "mmr": mmr,
                    "status": status,
                })

            rows.sort(key=self._room_row_sort_key)

            for row in rows:
                name = str(row["name"])
                mmr = int(row["mmr"])
                status = str(row["status"])
                if status == "placement":
                    lines.append(f"{name} Placement")
                elif status == "season_placement":
                    lines.append(f"{name} {mmr} MMR · Season Placement")
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

            mmr_map = await self._effective_display_mmrs(ordered)
            groups = [ordered[i:i + ROOM_CAPACITY] for i in range(0, len(ordered), ROOM_CAPACITY)]
            for idx, g in enumerate(groups, start=1):
                lines.append(f"ROOM {idx}")
                for m in g:
                    if self._has_season_placement_role(m):
                        lines.append(f"{m.display_name} {int(mmr_map.get(m.id, 0))} MMR · Season Placement")
                    elif self._is_placement_member(m):
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

        mmr_map = await self._effective_display_mmrs(ordered)

        label = self._next_start_minute_label(now)
        lines = [f"----------------- Future Room (starting at {label})"]
        for m in ordered:
            label = await self._mmr_label_for_member(m, int(mmr_map.get(m.id, 0)), with_unit=True)
            lines.append(f"{m.display_name} ({label})")

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
            # user_id -> snapshot visual. Esto permite mantener al jugador en la sala
            # aunque salga del servidor y Discord lo saque automáticamente del hilo.
            "player_snapshots": {},
            # user_ids que salieron del servidor, pero siguen contando para submit.
            "left_server_user_ids": set(),
            "phase": PHASE_FUTURE,  # FUTURE: thread listo, songs aún NO
            "locked_in_thread": True,  # si quieres que estén bloqueados desde PREP
            "started": False,
            "finished": False,
            "created_ts": int(self._now_lima().timestamp()),
        }
        rid = self._next_room_id()
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

    def _remember_room_player_snapshot(self, info: dict, member: discord.Member):
        """
        Guarda datos mínimos del jugador para que la sala no pierda su identidad
        si sale del servidor antes del submit.
        """
        snapshots = info.setdefault("player_snapshots", {})
        snapshots[int(member.id)] = {
            "display_name": member.display_name,
            "mention": member.mention,
            "is_placement": bool(self._is_placement_member(member)),
            "is_season_placement": bool(self._has_season_placement_role(member)),
        }

    def _replace_room_player_object(self, info: dict, member: discord.Member) -> bool:
        """
        Reemplaza el objeto Member viejo por el nuevo cuando el usuario reingresa.
        Devuelve True si el usuario ya estaba listado en la sala.
        """
        players = info.setdefault("players", [])
        for i, p in enumerate(players):
            if int(getattr(p, "id", 0) or 0) == int(member.id):
                players[i] = member
                self._remember_room_player_snapshot(info, member)
                info.setdefault("left_server_user_ids", set()).discard(int(member.id))
                return True
        return False

    async def _add_member_to_room(self, member: discord.Member, rid: int, info: dict):
        players = info.setdefault("players", [])

        # Comparar por ID, no por identidad de objeto. Si el usuario salió y volvió,
        # Discord entrega otro objeto Member, pero sigue siendo el mismo jugador.
        existing_idx = next(
            (
                i
                for i, p in enumerate(players)
                if int(getattr(p, "id", 0) or 0) == int(member.id)
            ),
            None,
        )

        if existing_idx is None:
            players.append(member)
        else:
            players[existing_idx] = member

        self._remember_room_player_snapshot(info, member)
        info.setdefault("left_server_user_ids", set()).discard(int(member.id))

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
        players = info.setdefault("players", [])
        before = len(players)
        info["players"] = [
            p for p in players
            if int(getattr(p, "id", 0) or 0) != int(member.id)
        ]
        if len(info["players"]) == before:
            return

        try:
            info.setdefault("player_snapshots", {}).pop(int(member.id), None)
            info.setdefault("left_server_user_ids", set()).discard(int(member.id))
        except Exception:
            pass

        try:
            await self.bot.get_channel(info["join_channel_id"]).get_thread(
                info["thread"]
            ).remove_user(member)
        except Exception:
            pass
        await self.update_room_board(member.guild, rid)

    async def _reattach_member_to_active_room_threads(self, member: discord.Member):
        """
        Si un usuario salió del servidor estando locked/started en una sala, Discord
        lo saca del hilo privado. Al reingresar, lo volvemos a agregar al hilo
        original y NO se crea una sala/queue nueva.
        """
        touched = False

        for rid, info in list(self.rooms.items()):
            if info.get("finished", False):
                continue

            players = info.get("players", []) or []
            if not any(int(getattr(p, "id", 0) or 0) == int(member.id) for p in players):
                continue

            self._replace_room_player_object(info, member)

            thread = self.bot.get_channel(info["join_channel_id"]).get_thread(info["thread"])
            if thread is None:
                continue

            try:
                await thread.add_user(member)
            except Exception:
                pass

            try:
                await thread.send(
                    f"✅ {member.mention} rejoined the server and was added back to this room.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except Exception:
                pass

            try:
                await self.update_room_board(member.guild, rid)
            except Exception:
                pass

            try:
                self.bot.dispatch("room_updated", rid)
            except Exception:
                pass

            touched = True

        if touched:
            try:
                self.bot.dispatch("room_updated", None)
            except Exception:
                pass

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

    @staticmethod
    def _song_title_key(title: str) -> str:
        """
        Normaliza títulos para ban robusto:
        - minúsculas
        - elimina signos/puntuación
        - colapsa espacios
        Así "What Sort of Ending Are You Wishing For?" y variantes sin ? matchean igual.
        """
        raw = str(title or "").strip().lower()
        raw = re.sub(r"[^0-9a-z]+", " ", raw)
        return re.sub(r"\s+", " ", raw).strip()

    @staticmethod
    def _normalize_song_server(server: str | None) -> str:
        value = str(server or "all").strip().lower()
        return value if value in VALID_SONG_SERVERS else "all"

    @staticmethod
    def _normalize_song_diff(diff: str | None) -> str | None:
        if diff is None:
            return None
        value = str(diff or "").strip().lower()
        if value in ("", "all", "*", "any"):
            return None
        return value if value in VALID_SONG_DIFFS else None

    @staticmethod
    def _song_key_sql(alias: str = "s") -> str:
        return (
            "lower(trim(regexp_replace("
            f"regexp_replace(coalesce({alias}.title, ''), '[^0-9A-Za-z]+', ' ', 'g'), "
            "'\\s+', ' ', 'g')))"
        )

    def _banned_song_filter_sql(self, alias: str = "s") -> str:
        key_expr = self._song_key_sql(alias)
        return f"""
        AND NOT EXISTS (
            SELECT 1
            FROM banned_songs bs
            WHERE bs.active = true
              AND (bs.server = 'all' OR bs.server = {alias}.server)
              AND (bs.diff IS NULL OR bs.diff = {alias}.diff)
              AND (
                    (bs.song_id IS NOT NULL AND bs.song_id = {alias}.id)
                    OR bs.title_key = {key_expr}
              )
        )
        """

    async def _seed_default_banned_songs(self, conn):
        """
        Persistencia real en DB. No depende de memoria.
        Se ejecuta en cog_load y deja activos los bans requeridos por defecto.
        """
        for title in DEFAULT_BANNED_QUEUE_SONGS:
            title_key = self._song_title_key(title)
            existing = await conn.fetchrow(
                """
                SELECT id
                FROM banned_songs
                WHERE active = true
                  AND server = 'all'
                  AND title_key = $1
                  AND diff IS NULL
                LIMIT 1
                """,
                title_key,
            )
            if existing:
                await conn.execute(
                    """
                    UPDATE banned_songs
                    SET title = $1,
                        reason = 'Default queue ban',
                        active = true
                    WHERE id = $2
                    """,
                    title,
                    existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO banned_songs
                        (song_id, title, title_key, diff, server, reason, created_by, active)
                    VALUES
                        (NULL, $1, $2, NULL, 'all', 'Default queue ban', NULL, true)
                    """,
                    title,
                    title_key,
                )

    async def _safe_channel_send(self, channel, *args, **kwargs):
        if channel is None:
            return None
        async with self._discord_io_sem:
            try:
                return await channel.send(*args, **kwargs)
            except discord.HTTPException as e:
                log.warning("Discord send failed: %s", e)
                return None
            except Exception:
                log.exception("Unexpected Discord send failure")
                return None

    def _get_thread_for_room(self, room_info: dict) -> discord.Thread | None:
        parent = self.bot.get_channel(room_info.get("join_channel_id"))
        if not parent:
            return None
        try:
            return parent.get_thread(int(room_info.get("thread") or 0))
        except Exception:
            return None

    async def _safe_thread_send(self, room_info: dict, *args, **kwargs):
        return await self._safe_channel_send(self._get_thread_for_room(room_info), *args, **kwargs)

    def _room_player_ids(self, room_info: dict) -> set[int]:
        ids: set[int] = set()
        for p in room_info.get("players", []) or []:
            uid = getattr(p, "id", None)
            if uid is None and isinstance(p, int):
                uid = p
            if uid is None:
                continue
            try:
                ids.add(int(uid))
            except (TypeError, ValueError):
                continue
        return ids

    def _room_members(self, guild: discord.Guild | None, room_info: dict) -> list[discord.Member]:
        members: list[discord.Member] = []
        for p in room_info.get("players", []) or []:
            member = p if isinstance(p, discord.Member) else None
            if member is None and guild is not None:
                uid = getattr(p, "id", p if isinstance(p, int) else None)
                if uid is not None:
                    member = guild.get_member(int(uid))
            if member and not member.bot and member.id not in {m.id for m in members}:
                members.append(member)
        return members

    async def _send_accuracy_card_reminder_once(self, room_info: dict, started_ts: int | None = None):
        """
        Se manda solo al iniciar en minuto :00 o :30 y solo una vez por room.
        El flag se marca antes del envío para evitar duplicados ante reintentos.
        """
        if room_info.get("accuracy_reminder_sent"):
            return

        ts = int(started_ts or room_info.get("started_at") or 0)
        if ts <= 0:
            return

        try:
            minute = dt.datetime.fromtimestamp(ts, tz=LIMA_TZ).minute
        except Exception:
            minute = self._now_lima().minute

        if minute not in (0, 30):
            return

        room_info["accuracy_reminder_sent"] = True
        await self._safe_thread_send(room_info, ACCURACY_CARD_REMINDER)

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
        en [low, high] y diffs dadas, filtrando por server y por banned_songs.
        """
        if limit <= 0 or not diffs:
            return []

        server = self._normalize_song_server(server)
        if server == "all":
            server = "en"

        exclude_keys = [
            self._song_title_key(t)
            for t in (exclude_titles or set())
            if self._song_title_key(t)
        ] or ["__none__"]

        key_expr = self._song_key_sql("s")
        banned_filter = self._banned_song_filter_sql("s")

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT s.title, s.level, s.diff, s.notes_count
                    FROM songs s
                    WHERE s.level BETWEEN $1 AND $2
                      AND s.server = $3
                      AND s.diff = ANY($4::text[])
                      AND {key_expr} <> ALL($5::text[])
                      {banned_filter}
                    ORDER BY random()
                    LIMIT $6
                    """,
                    int(low),
                    int(high),
                    server,
                    list(diffs),
                    exclude_keys,
                    int(limit),
                )

        return [
            (
                r["title"],
                int(r["level"]),
                r["diff"].capitalize(),
                int(r["notes_count"]),
            )
            for r in rows
        ]

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
        - Intenta sacar hasta `per_level` canciones por nivel usando `diffs`.
        - Respeta `exclude_titles`, banned_songs y nunca duplica títulos.
        """
        pool: list[tuple[str, int, str, int]] = []
        used_keys: set[str] = {
            self._song_title_key(t)
            for t in (exclude_titles or set())
            if self._song_title_key(t)
        }

        server = self._normalize_song_server(server)
        if server == "all":
            server = "en"

        key_expr = self._song_key_sql("s")
        banned_filter = self._banned_song_filter_sql("s")

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                for lvl in range(int(low), int(high) + 1):
                    rows = await conn.fetch(
                        f"""
                        SELECT s.title, s.level, s.diff, s.notes_count
                        FROM songs s
                        WHERE s.level = $1
                          AND s.server = $2
                          AND s.diff = ANY($3::text[])
                          AND {key_expr} <> ALL($4::text[])
                          {banned_filter}
                        ORDER BY random()
                        LIMIT $5
                        """,
                        lvl,
                        server,
                        list(diffs),
                        list(used_keys) or ["__none__"],
                        int(per_level),
                    )

                    for r in rows:
                        title = str(r["title"])
                        title_key = self._song_title_key(title)
                        if title_key in used_keys:
                            continue

                        pool.append(
                            (
                                title,
                                int(r["level"]),
                                r["diff"].capitalize(),
                                int(r["notes_count"]),
                            )
                        )
                        used_keys.add(title_key)

        return pool

    async def _pick_placement_songs_levels_24_to_28(
        self, *, server: str = "en"
    ) -> list[tuple[str, int, str, int]]:
        """
        Regla especial Placement:
        - EXACTAMENTE 1 canción por nivel: 24, 25, 26, 27 y 28
        - Solo EXPERT
        - Evita repetir títulos
        - Respeta banned_songs.
        """
        levels = [24, 25, 26, 27, 28]
        picks: list[tuple[str, int, str, int]] = []
        chosen_keys: set[str] = set()

        server = self._normalize_song_server(server)
        if server == "all":
            server = "en"

        key_expr = self._song_key_sql("s")
        banned_filter = self._banned_song_filter_sql("s")

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                for L in levels:
                    rows = await conn.fetch(
                        f"""
                        SELECT s.title, s.level, s.diff, s.notes_count
                        FROM songs s
                        WHERE s.level = $1
                          AND s.diff = 'expert'
                          AND s.server = $2
                          AND {key_expr} <> ALL($3::text[])
                          {banned_filter}
                        ORDER BY random()
                        LIMIT 5
                        """,
                        int(L),
                        server,
                        list(chosen_keys) or ["__none__"],
                    )

                    picked = None
                    for r in rows:
                        if self._song_title_key(r["title"]) not in chosen_keys:
                            picked = r
                            break

                    if picked is None:
                        fb = await conn.fetch(
                            f"""
                            SELECT s.title, s.level, s.diff, s.notes_count
                            FROM songs s
                            WHERE s.level BETWEEN 24 AND 28
                              AND s.diff = 'expert'
                              AND s.server = $1
                              AND {key_expr} <> ALL($2::text[])
                              {banned_filter}
                            ORDER BY random()
                            LIMIT 10
                            """,
                            server,
                            list(chosen_keys) or ["__none__"],
                        )
                        for r in fb:
                            if self._song_title_key(r["title"]) not in chosen_keys:
                                picked = r
                                break

                    if picked:
                        chosen_keys.add(self._song_title_key(picked["title"]))
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
        val = (season_choice or "current").strip()
        key = val.lower()

        if key in ("current", "actual"):
            row = await self.get_current_season()
            return (row["id"], row["name"]) if row else (None, "N/A")

        aliases = {
            "season 3": ["season 3"],
            "season 2": ["season 2"],
            "season 1": ["season 1"],
            "pre season": ["pre season"],
            "beta test season": ["beta test season"],
            "alpha test season": ["alpha test season"],
        }
        candidate_names = aliases.get(key, [val])

        async with self.db_pool.acquire() as conn:
            where = " OR ".join(
                f"LOWER(name) = ${i+1}" for i in range(len(candidate_names))
            )
            row = await conn.fetchrow(
                f"""
                SELECT id, name
                FROM seasons
                WHERE {where}
                ORDER BY startdate DESC
                LIMIT 1
                """,
                *[c.lower() for c in candidate_names],
            )

            if not row and key.isdigit():
                row = await conn.fetchrow(
                    "SELECT id, name FROM seasons WHERE id = $1",
                    int(key),
                )

        return (row["id"], row["name"]) if row else (None, val)

    async def _fetch_user_stats(
        self,
        guild: discord.Guild,
        user: discord.Member,
        season_id: int,
    ):
        leaderboard = await self._build_visible_leaderboard_rows(guild, season_id)
        total = len(leaderboard)

        target_idx = next(
            (i for i, row in enumerate(leaderboard) if int(row["user_id"]) == int(user.id)),
            None,
        )
        if target_idx is None:
            return None

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    sp.mmr,
                    sp.role,
                    COALESCE(p.country, '') AS country
                FROM seasonplayers sp
                LEFT JOIN players p ON p.user_id = sp.user_id
                WHERE sp.season_id = $1 AND sp.user_id = $2
                """,
                season_id,
                user.id,
            )
            if not row:
                return None

            mmr = int(row["mmr"])
            role = str(row["role"])

            matches_played = (
                await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM match_history
                    WHERE season_id = $1 AND user_id = $2
                    """,
                    season_id,
                    user.id,
                )
                or 0
            )

            recent_rows = await conn.fetch(
                """
                SELECT is_win, mmr_delta
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                ORDER BY played_at DESC, id DESC
                LIMIT 10
                """,
                season_id,
                user.id,
            )

            last10_wins = sum(1 for r in recent_rows if r["is_win"])
            last10_losses = sum(1 for r in recent_rows if not r["is_win"])
            last10_delta = sum(int(r["mmr_delta"]) for r in recent_rows) if recent_rows else 0

            # Peak MMR dentro de la season
            # Para placements (incluyendo season placement), la primera partida
            # debe contar desde mmr_after, no desde el seed/mmr_before.
            peak_mmr = await conn.fetchval(
                """
                SELECT MAX(
                    CASE
                        WHEN was_placement THEN mmr_after
                        ELSE GREATEST(mmr_before, mmr_after)
                    END
                )
                FROM match_history
                WHERE season_id = $1 AND user_id = $2
                """,
                season_id,
                user.id,
            )

            # Si no tiene histórico, usamos el MMR actual
            if peak_mmr is None:
                peak_mmr = mmr

        visible_mmr = await self._display_mmr_for_member(
            user,
            mmr,
            season_id,
            int(matches_played),
        )

        visible_peak = await self._display_peak_mmr_for_member(
            user,
            int(peak_mmr),
            season_id,
            int(matches_played),
        )

        return {
            "pos": target_idx + 1,
            "total": total,
            "mmr": visible_mmr,
            "role": "Season Placement" if await self._is_unplayed_season_placement(user, season_id, int(matches_played)) else role,
            "real_role": role,
            "is_season_placement": await self._is_unplayed_season_placement(user, season_id, int(matches_played)),
            "starting_mmr": visible_mmr if await self._is_unplayed_season_placement(user, season_id, int(matches_played)) else None,
            "mmr_label": "Starting MMR" if await self._is_unplayed_season_placement(user, season_id, int(matches_played)) else "MMR",
            "matches_played": int(matches_played),
            "last10_wins": last10_wins,
            "last10_losses": last10_losses,
            "last10_mmr_delta": last10_delta,
            "peak_mmr": visible_peak,
            "country_code": str(row["country"] or "").upper().strip(),
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

        players = [m for m in (info.get("players", []) or []) if m and not m.bot]
        snapshots = info.setdefault("player_snapshots", {})
        left_server_ids = set(info.get("left_server_user_ids") or set())

        # ✅ usa MMR “display” (SeasonPlacement => prev-50)
        mmr_map = await self._effective_display_mmrs(players)

        rows = []
        seen_ids: set[int] = set()
        for m in players:
            uid = int(m.id)
            if uid in seen_ids:
                continue
            seen_ids.add(uid)

            if self._has_season_placement_role(m):
                status = "season_placement"
            elif self._is_placement_member(m):
                status = "placement"
            else:
                status = "regular"

            rows.append({
                "user_id": uid,
                "name": m.display_name,
                "mmr": int(mmr_map.get(uid, 0)),
                "status": status,
            })

        rows.sort(key=self._room_row_sort_key)

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
            uid = int(r["user_id"])
            member = guild.get_member(uid)

            if member is not None:
                name = await self.resolve_member_name(guild, uid)
                label = await self._mmr_label_for_member(member, int(r["mmr"]), with_unit=True)
            else:
                snap = snapshots.get(uid) or snapshots.get(str(uid)) or {}
                name = str(snap.get("display_name") or f"ID:{uid}")
                if bool(snap.get("is_season_placement")):
                    label = f"{int(r['mmr'])} MMR · Season Placement"
                elif bool(snap.get("is_placement")):
                    label = "Placement"
                else:
                    label = f"{int(r['mmr'])} MMR"

            suffix = " — left server, kept for submit" if uid in left_server_ids else ""
            lines.append(f"• {name} ({label}){suffix}")

        ts = int(datetime.datetime.utcnow().timestamp())
        return header + ("\n" + "\n".join(lines) if lines else "") + f"\n\nLast Update <t:{ts}:R>"

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

    async def _build_cutoffs_data(self, season_id: int, tiers: tuple[int, ...]) -> list[dict]:
        """
        Misma lógica que la página:
        - ignora Placement 0 MMR y Season Placement sin jugar para cutoffs reales;
        - si no existe Top N porque hay menos jugadores, muestra Before Top N con el último jugador real;
        - mantiene N/A para los cutoffs no alcanzados.
        """
        assert tiers and all(int(t) >= 1 for t in tiers)

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH wanted AS (
                    SELECT unnest($2::int[])::int AS cutoff_position
                ), sp_one AS (
                    SELECT *
                    FROM (
                        SELECT
                            sp.*,
                            row_number() OVER (
                                PARTITION BY sp.season_id, sp.user_id
                                ORDER BY sp.mmr DESC, sp.ctid DESC
                            ) AS rn
                        FROM seasonplayers sp
                        WHERE sp.season_id = $1
                    ) x
                    WHERE rn = 1
                ), played AS (
                    SELECT user_id, COUNT(*)::int AS matches_played
                    FROM match_history
                    WHERE season_id = $1
                    GROUP BY user_id
                ), real_players AS (
                    SELECT
                        sp.user_id,
                        sp.mmr,
                        sp.role,
                        COALESCE(pl.matches_played, 0) AS matches_played,
                        row_number() OVER (ORDER BY sp.mmr DESC, sp.user_id ASC)::int AS pos
                    FROM sp_one sp
                    LEFT JOIN played pl ON pl.user_id = sp.user_id
                    WHERE NOT (COALESCE(pl.matches_played, 0) = 0 AND COALESCE(sp.mmr, 0) <= 0)
                      AND NOT (COALESCE(pl.matches_played, 0) = 0 AND COALESCE(sp.role, '') <> 'Placement' AND COALESCE(sp.mmr, 0) > 0)
                ), totals AS (
                    SELECT COUNT(*)::int AS total_players FROM real_players
                ), picked AS (
                    SELECT
                        w.cutoff_position,
                        t.total_players,
                        CASE
                            WHEN t.total_players <= 0 THEN NULL::int
                            WHEN t.total_players >= w.cutoff_position THEN w.cutoff_position
                            ELSE t.total_players
                        END AS picked_position
                    FROM wanted w
                    CROSS JOIN totals t
                )
                SELECT
                    p.cutoff_position,
                    p.total_players,
                    p.picked_position,
                    rp.user_id,
                    rp.mmr,
                    rp.role,
                    rp.pos
                FROM picked p
                LEFT JOIN real_players rp ON rp.pos = p.picked_position
                ORDER BY p.cutoff_position ASC
                """,
                int(season_id),
                list(int(t) for t in tiers),
            )

        out: list[dict] = []
        fallback_added = False
        for r in rows:
            cutoff = int(r["cutoff_position"] or 0)
            total = int(r["total_players"] or 0)
            picked_pos = r["picked_position"]
            mmr = r["mmr"]

            if total > 0 and picked_pos is not None and int(picked_pos) == cutoff:
                out.append({"top": cutoff, "label": f"Top {cutoff}", "mmr": int(mmr), "missing": False})
                continue

            if total > 0 and picked_pos is not None and not fallback_added:
                out.append({
                    "top": cutoff,
                    "label": f"Before Top {cutoff}",
                    "mmr": int(mmr),
                    "missing": False,
                    "actual_pos": int(picked_pos),
                })
                fallback_added = True

            out.append({"top": cutoff, "label": f"Top {cutoff}", "mmr": None, "missing": True})

        return out

    def _format_cutoffs_board(self, season_name: str, rows: list[dict]) -> str:
        lines: list[str] = []
        lines.append(f"MMR cutoffs — {season_name}")
        lines.append("```")
        lines.append("Cutoff          |   MMR")
        lines.append("----------------+------")
        for row in rows:
            label = str(row.get("label") or f"Top {row.get('top')}")
            if row.get("actual_pos"):
                label = f"{label} ({row['actual_pos']})"
            mmr = row.get("mmr")
            mmr_str = str(mmr) if mmr is not None else "N/A"
            lines.append(f"{label:<15}| {mmr_str:>6}")
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
        self.db_pool: asyncpg.Pool | None = None
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
        self._join_lock = asyncio.Lock()
        self._room_locks: dict[int, asyncio.Lock] = {}
        self._forfeit_locks: dict[int, asyncio.Lock] = {}
        self._discord_io_sem = asyncio.Semaphore(DISCORD_IO_CONCURRENCY)
        self._closing_threads: set[int] = set()

        self.rooms: dict[int, dict] = {}
        try:
            if os.path.isfile("assets/cached_rooms.json"):
                with open("assets/cached_rooms.json", "r", encoding="utf-8") as f:
                    self.rooms = self._normalize_cached_rooms(json.load(f))
        except Exception:
            self.rooms = {}

        self._sched_last_minute = {}  # parent_id -> minute_ts
        self._sched_last_slot = {}  # parent_id -> (y,m,d,hm)

    def _normalize_cached_rooms(self, raw_rooms) -> dict[int, dict]:
        """
        json.load convierte las keys numéricas del dict en strings.
        Si luego hacemos max(self.rooms.keys()) + 1, explota con:
        TypeError: can only concatenate str (not "int") to str

        Esta normalización evita eso y además sanea campos numéricos.
        """
        if not isinstance(raw_rooms, dict):
            return {}

        normalized: dict[int, dict] = {}

        for raw_rid, raw_info in raw_rooms.items():
            try:
                rid = int(raw_rid)
            except (TypeError, ValueError):
                continue

            if not isinstance(raw_info, dict):
                continue

            info = dict(raw_info)

            for field in (
                "thread",
                "join_channel_id",
                "created_ts",
                "display_num",
                "slot_start_ts",
                "avg_mmr",
                "started_at",
            ):
                value = info.get(field)
                if value in (None, ""):
                    continue
                try:
                    info[field] = int(value)
                except (TypeError, ValueError):
                    pass

            # No se puede reconstruir discord.Member desde JSON.
            # Dejamos players vacío para evitar estados corruptos tras reinicio.
            info["players"] = []

            info.setdefault("server", "en")
            info.setdefault("phase", PHASE_FUTURE)
            info.setdefault("locked_in_thread", False)
            info["started"] = bool(info.get("started", False))
            info["finished"] = bool(info.get("finished", False))

            normalized[rid] = info

        return normalized

    def _next_room_id(self) -> int:
        numeric_ids: list[int] = []
        for rid in self.rooms.keys():
            try:
                numeric_ids.append(int(rid))
            except (TypeError, ValueError):
                continue

        return (max(numeric_ids) + 1) if numeric_ids else 1

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

    @tasks.loop(seconds=1.0)
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
                prep_start = self._in_prep_window(now)
                if prep_start is not None:
                    # Durante PREP solo se crean rooms si TODA la queue está completa
                    # en múltiplos exactos de 5: 5, 10, 15...
                    # Si hay 6-9, 11-14, etc., NO se crea room todavía;
                    # se espera hasta el slot para crear solo las completas y dropear sobrantes.
                    await self._prepare_rooms_for_join_channel(
                        guild,
                        join_channel,
                        require_exact_multiple=True,
                    )

                    # ✅ Mantener anuncios 1 vez por minuto (anti-spam)
                    minute_key = (now.year, now.month, now.day, now.hour, now.minute)
                    if self._prep_last_minute.get(join_id) != minute_key:
                        self._prep_last_minute[join_id] = minute_key

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

                        # (1) Al llegar :00/:30, crea SOLO las rooms completas.
                        # Ej: 6 => crea 1 room y deja 1 en queue para dropearlo abajo.
                        # Ej: 9 => crea 1 room y deja 4 en queue para dropearlos abajo.
                        await self._prepare_rooms_for_join_channel(
                            guild,
                            join_channel,
                            slot_dt=slot_dt,
                            require_exact_multiple=False,
                        )

                        # (2) PURGE: saca a los que quedaron en grupo incompleto al llegar el slot
                        await self._purge_unfilled_queue_on_slot(guild, join_channel, now)

                        # (3) START: inicia rooms FUTURE programadas para este slot
                        await self._start_future_rooms_for_slot(join_id, slot_ts)

                        # (4) ANNOUNCE: publica los próximos horarios (con timestamps)
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
    async def on_member_join(self, member: discord.Member):
        # Discord borra todos los roles cuando alguien sale del servidor.
        # Al volver, restauramos el rol competitivo usando seasonplayers como fuente de verdad.
        if member.guild.id != GUILD_ID:
            return

        try:
            await self._sync_member_rank_role_from_current_season(
                member.guild,
                member,
                reason="Member rejoined -> sync current-season rank role",
            )
        except Exception:
            pass

        # Si salió del servidor estando en una room, NO perdió su slot.
        # Al volver, lo agregamos al hilo original y actualizamos su objeto Member.
        try:
            await self._reattach_member_to_active_room_threads(member)
        except Exception:
            pass

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
        # (A) Si se fue del servidor:
        #     - si estaba en queue normal, sí se saca de la queue.
        #     - si ya estaba en room/locked thread, NO se remueve de la sala.
        #       Debe seguir contando como jugador hasta submit.
        # =========================================================
        try:
            async with self._join_lock:
                # A1) Remover de cualquier queue EN/JP porque todavía no tenía slot locked.
                for join_id in list(ALLOWED_JOIN_CHANNELS):
                    q = self.queues.get(join_id, set())
                    if member.id in q:
                        q.discard(member.id)
                        self.queue_join_order.get(join_id, {}).pop(member.id, None)

                        ch = member.guild.get_channel(join_id)
                        if isinstance(ch, discord.TextChannel):
                            try:
                                qcount = len(q)
                                await ch.send(
                                    f"(<@{member.id}>) Dropped the queue (`{qcount}`)"
                                )
                            except Exception:
                                pass

                # A2) Si ya estaba dentro de una room, mantenerlo en info["players"].
                # Discord puede sacarlo automáticamente del hilo privado al salir del server,
                # pero el bot NO debe hacer remove_user, NO debe bajar 5/5 a 4/5 y NO debe
                # rebalancear. El submit seguirá exigiendo sus 5 líneas.
                for rid, info in list(self.rooms.items()):
                    if info.get("finished", False):
                        continue

                    players = info.get("players", []) or []
                    hit = next(
                        (p for p in players if int(getattr(p, "id", 0) or 0) == int(member.id)),
                        None,
                    )
                    if not hit:
                        continue

                    try:
                        if isinstance(hit, discord.Member):
                            self._remember_room_player_snapshot(info, hit)
                    except Exception:
                        pass

                    info.setdefault("left_server_user_ids", set()).add(int(member.id))

                    thread = self.bot.get_channel(info["join_channel_id"]).get_thread(info["thread"])
                    if thread is not None:
                        try:
                            await thread.send(
                                f"⚠️ <@{member.id}> left the server, but remains listed in this room until submit. "
                                f"If they rejoin, I will add them back to this thread.",
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                            )
                        except Exception:
                            pass

                    try:
                        await self.update_room_board(member.guild, rid)
                    except Exception:
                        pass

                    try:
                        self.bot.dispatch("room_updated", rid)
                    except Exception:
                        pass

                try:
                    self.bot.dispatch("room_updated", None)
                except Exception:
                    pass

        except Exception:
            # No bloqueamos el resto del cleanup si falla algo acá
            pass

        # Si estaba en una room activa, no hacemos cleanup de DB.
        # Debe permanecer como jugador válido hasta que la sala haga submit/forfeit.
        try:
            still_listed_in_room = any(
                not info.get("finished", False)
                and any(
                    int(getattr(p, "id", 0) or 0) == int(member.id)
                    for p in (info.get("players", []) or [])
                )
                for info in self.rooms.values()
            )
            if still_listed_in_room:
                return
        except Exception:
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
        if self.db_pool is None:
            self.db_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=DB_POOL_MIN_SIZE,
                max_size=DB_POOL_MAX_SIZE,
                command_timeout=DB_COMMAND_TIMEOUT,
            )

        async with self.db_pool.acquire() as conn:
            # Jugadores
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    user_id BIGINT PRIMARY KEY,
                    mmr     INTEGER NOT NULL DEFAULT 0,
                    role    TEXT    DEFAULT 'Placement',
                    country TEXT    DEFAULT ''
                );
                """
            )

            await conn.execute(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS mmr INTEGER NOT NULL DEFAULT 0;"
            )
            await conn.execute(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'Placement';"
            )
            await conn.execute(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS country TEXT DEFAULT '';"
            )

        # deja debajo el resto de tu cog_load tal como ya lo tienes
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

            # Baneos persistentes de canciones de queue.
            # Soporta ban por título normalizado, por song_id, por server y por dificultad.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS banned_songs (
                    id         BIGSERIAL PRIMARY KEY,
                    song_id    INTEGER,
                    title      TEXT NOT NULL,
                    title_key  TEXT NOT NULL,
                    diff       TEXT,
                    server     TEXT NOT NULL DEFAULT 'all',
                    reason     TEXT,
                    created_by BIGINT,
                    active     BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_banned_songs_lookup
                ON banned_songs (server, title_key, diff)
                WHERE active = true;
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_banned_songs_song_id
                ON banned_songs (song_id, server, diff)
                WHERE active = true AND song_id IS NOT NULL;
                """
            )

            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_banned_songs_active
                ON banned_songs (server, title_key, COALESCE(diff, '*'))
                WHERE active = true;
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_songs_select_fast
                ON songs (server, diff, level);
                """
            )

            await self._seed_default_banned_songs(conn)

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

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS global_settings (
                    key   TEXT PRIMARY KEY,
                    value BIGINT NOT NULL
                );
                """
            )

            #Season Reset
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seasons (
                    id        SERIAL PRIMARY KEY,
                    name      TEXT NOT NULL,
                    active    BOOLEAN NOT NULL DEFAULT false,
                    startdate TIMESTAMPTZ NOT NULL DEFAULT now(),
                    enddate   TIMESTAMPTZ
                );
                """
            )

            # SeasonPlayers
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seasonplayers (
                    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
                    user_id   BIGINT  NOT NULL REFERENCES players(user_id) ON DELETE CASCADE,
                    mmr       INTEGER NOT NULL DEFAULT 0,
                    role      TEXT    NOT NULL DEFAULT 'Placement',
                    PRIMARY KEY (season_id, user_id)
                );
                """
            )

            # Blindaje para bases antiguas donde seasonplayers pudo existir sin PK.
            # Primero se limpian duplicados técnicos exactos por season+user, luego
            # se crea índice único para que ON CONFLICT(season_id,user_id) sea confiable.
            await conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        ctid,
                        row_number() OVER (
                            PARTITION BY season_id, user_id
                            ORDER BY mmr DESC, ctid DESC
                        ) AS rn
                    FROM seasonplayers
                )
                DELETE FROM seasonplayers sp
                USING ranked r
                WHERE sp.ctid = r.ctid
                  AND r.rn > 1;
                """
            )

            await conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_seasonplayers_season_user
                ON seasonplayers(season_id, user_id);
                """
            )

            await conn.execute(
                """
                ALTER TABLE seasonplayers
                  ADD COLUMN IF NOT EXISTS is_season_placement BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE seasonplayers
                  ADD COLUMN IF NOT EXISTS starting_mmr INTEGER;
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seasonplayers_season_mmr ON seasonplayers(season_id, mmr DESC);"
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
                # Reemplazo atómico por servidor.
                # Si un refresh falla o genera 0 rows, NO borramos el catálogo anterior.
                async with conn.transaction():
                    for server_code, rows in by_server.items():
                        if not rows:
                            log.warning(
                                f"[songs] No hay canciones válidas para server={server_code} tras filtrar por release+1d. Se conserva el catálogo anterior."
                            )
                            continue

                        await conn.execute(
                            "DELETE FROM songs WHERE server = $1", server_code
                        )

                        await conn.executemany(
                            """
                            INSERT INTO songs (id, title, diff, level, notes_count, server)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            ON CONFLICT (id, diff, server) DO UPDATE SET
                                title = EXCLUDED.title,
                                level = EXCLUDED.level,
                                notes_count = EXCLUDED.notes_count
                            """,
                            rows,
                        )

                    await self._seed_default_banned_songs(conn)

        print(
            "[songs] Catálogo EN/JP actualizado (reemplazo por servidor, con filtro release+1d)"
        )

    @refresh_songs.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def _get_10_songs(self, low, high, diffs=None):
        if diffs is None:
            diffs = DIFFICULTY_PRIORITY

        key_expr = self._song_key_sql("s")
        banned_filter = self._banned_song_filter_sql("s")

        async with self._songs_lock:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT s.title, s.level, s.diff, s.notes_count
                    FROM songs s
                    WHERE s.level BETWEEN $1 AND $2
                      AND s.diff = ANY($3::text[])
                      {banned_filter}
                    """,
                    int(low),
                    int(high),
                    list(diffs),
                )

        songs_by_lvl = {}
        for r in rows:
            lvl = r["level"]
            songs_by_lvl.setdefault(lvl, []).append(
                (r["title"], lvl, r["diff"].capitalize(), int(r["notes_count"]))
            )

        picks = []
        for lvl in range(high, low - 1, -1):
            pool = songs_by_lvl.get(lvl, [])
            if not pool:
                continue
            k = min(3, len(pool))
            picks.extend(random.sample(pool, k))

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
        Lockdown durante PREP (:25-:29 / :55-:59).

        Regla final:
        - Si ya existen rooms locked para el slot actual, nadie más puede usar /c.
        - Si todavía no se crearon rooms, pero la queue está exactamente completa
          en múltiplos de 5 (5, 10, 15...), se bloquea el siguiente /c.
        - Si hay sobrantes (6-9, 11-14...), se permite /c para intentar
          completar la siguiente room antes del slot.
        """
        if not self._lockdown_window(now):
            return False

        slot_ts = self._current_prep_slot_ts(now)
        if slot_ts is not None and self._count_locked_rooms_for_slot(join_id, slot_ts) > 0:
            return True

        ordered = await self._get_ordered_queue_members(guild, join_id)
        qlen = len(ordered)

        if qlen >= ROOM_CAPACITY and qlen % ROOM_CAPACITY == 0:
            return True

        return False

    async def launch_song_poll(self, room_info):
        thread_id = int(room_info.get("thread") or 0)
        if thread_id <= 0:
            log.warning("launch_song_poll called without valid thread id")
            return

        lock = self._room_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            try:
                return await asyncio.wait_for(
                    self._launch_song_poll_impl(room_info),
                    timeout=SONG_POLL_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                log.exception("Song poll timed out for thread_id=%s", thread_id)
                await self._safe_thread_send(
                    room_info,
                    "⚠️ Song selection timed out. Try reroll or contact staff.",
                )
            except Exception:
                log.exception("Song poll failed for thread_id=%s", thread_id)
                await self._safe_thread_send(
                    room_info,
                    "⚠️ Song selection failed safely. No MMR/result logic was changed.",
                )

    async def _launch_song_poll_impl(self, room_info):
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
        has_rank_placement = counts.get("Placement", 0) > 0

        if has_rank_placement:
            # SOLO Placement legacy usa 24–28
            base_rank = "Placement"
        else:
            # Season Placement usa MMR “display” (prev-100), igual que el board
            mmr_map = await self._effective_display_mmrs([m for m in players if m and not m.bot])
            avg_mmr = round(sum(int(mmr_map.get(m.id, 0)) for m in players) / len(players)) if players else 0
            base_rank = get_rank_from_mmr(avg_mmr)
            if base_rank not in BRACKET_RANGES:
                base_rank = "Diamond"

        low, high = BRACKET_RANGES[base_rank]
        allowed_diffs = ALLOWED_DIFFS_BY_RANK.get(base_rank, DIFFICULTY_PRIORITY)

        # 3) Selección de canciones
        if base_rank == "Placement":
            picks = await self._pick_placement_songs_levels_24_to_28(server=server_code)
            low, high = 24, 28

            # ✅ Asegurar exactamente 5 (o lo más cercano posible) sin crashear
            final_picks = list(picks)
            if len(final_picks) < 5:
                extra = await self._get_random_songs(
                    24, 28, ("expert",),
                    limit=5 - len(final_picks),
                    server=server_code,
                    exclude_titles={t for (t, *_r) in final_picks},
                )
                final_picks.extend(extra)

            # Si por algún motivo hay >5, recorta sin ValueError
            if len(final_picks) > 5:
                final_picks = secrets.SystemRandom().sample(final_picks, k=5)

        else:
            base_lo, base_hi = low, high
            non_append_diffs = tuple(d for d in allowed_diffs if d != "append")

            append_range = None
            if "append" in allowed_diffs:
                append_range = APPEND_RANGES.get(base_rank)

            pool_non_append: list[tuple[str, int, str, int]] = []
            pool_append: list[tuple[str, int, str, int]] = []

            taken_titles: set[str] = set()
            if non_append_diffs:
                pool_non_append = await self._build_level_balanced_pool(
                    base_lo,
                    base_hi,
                    server=server_code,
                    diffs=non_append_diffs,
                    per_level=5,
                )
                taken_titles = {t for (t, *_rest) in pool_non_append}

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

            prob = get_append_probability_for_rank(base_rank)
            used_append = False
            picks: list[tuple[str, int, str, int]] = []

            if pool_append and random.random() < prob:
                used_append = True
                random.shuffle(pool_append)
                random.shuffle(pool_non_append)

                append_count = pick_append_count_for_rank(base_rank, max_available=len(pool_append))
                append_count = max(1, min(append_count, 5))

                picks.extend(pool_append[:append_count])
                remaining = 5 - len(picks)

                if remaining > 0 and pool_non_append:
                    picks.extend(pool_non_append[:remaining])
                    remaining = 5 - len(picks)

                if remaining > 0 and len(pool_append) > append_count:
                    picks.extend(pool_append[append_count : append_count + remaining])

            else:
                random.shuffle(pool_non_append)
                picks = pool_non_append[:5]
                remaining = 5 - len(picks)

                if remaining > 0 and pool_append:
                    used_append = True
                    random.shuffle(pool_append)
                    picks.extend(pool_append[:remaining])

            # ✅ Relleno final si el catálogo no alcanza (evita ValueError)
            final_picks = list(picks)
            if len(final_picks) < 5:
                # fallback: usa allowed_diffs (sin reventar SQL)
                fb_diffs = tuple(d for d in allowed_diffs if d != "append") or ("expert", "master", "hard", "normal")
                extra = await self._get_random_songs(
                    base_lo, base_hi,
                    fb_diffs,
                    limit=5 - len(final_picks),
                    server=server_code,
                    exclude_titles={t for (t, *_r) in final_picks},
                )
                final_picks.extend(extra)

            # Si aún así no llegó a 5, no crashees: manda lo que hay
            if len(final_picks) > 5:
                final_picks = secrets.SystemRandom().sample(final_picks, k=5)

            # e) Decidir gap a mostrar
            if used_append and append_range is not None:
                alo, ahi = append_range
                low = min(base_lo, alo)
                high = max(base_hi, ahi)
            else:
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
        
    async def _get_previous_season(self, current_season_id: int) -> asyncpg.Record | None:
        async with self.db_pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT id, name, active, startdate, enddate
                FROM seasons
                WHERE id <> $1
                ORDER BY enddate DESC NULLS LAST, startdate DESC
                LIMIT 1
                """,
                int(current_season_id),
            )
            
    async def _fetch_previous_season_mmr(self, user_id: int, prev_season_id: int | None) -> int:
        if not prev_season_id:
            return 0
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT mmr
                FROM seasonplayers
                WHERE user_id = $1 AND season_id = $2
                """,
                int(user_id),
                int(prev_season_id),
            )
        return int(row["mmr"]) if row else 0
    
    async def _fetch_mmrs_batch_for_season(self, season_id: int, user_ids: list[int]) -> dict[int, int]:
        if not user_ids:
            return {}
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, mmr
                FROM seasonplayers
                WHERE season_id = $1
                AND user_id = ANY($2::bigint[])
                """,
                int(season_id),
                user_ids,
            )
        out = {int(r["user_id"]): int(r["mmr"]) for r in rows}
        for uid in user_ids:
            out.setdefault(uid, 0)
        return out

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

        await interaction.response.defer(ephemeral=True)

        member: discord.Member = interaction.user  # type: ignore

        # Fail-safe: si el usuario salió del servidor, Discord le borra los roles.
        # Al volver o al usar /c, sincronizamos su rol desde seasonplayers.
        # IMPORTANTE: si ya trae Season Placement, NO se reemplaza por Gold/Rank aquí.
        # Ese rol solo debe salir después de un submit aprobado.
        try:
            await self._sync_member_rank_role_from_current_season(
                guild,
                member,
                reason="Sync current-season role before queue join",
                preserve_existing_season_placement=True,
            )
        except Exception:
            pass

        first_prep_ping_role_id: int | None = None

        async with self._join_lock:
            # Guard: ya estás en una sala activa (cualquier fase no-finished)
            already_room = self._find_player_room_id(member)
            if already_room is not None:
                return await interaction.followup.send(
                    "❌ You are already in a room. You cannot queue again until that room finishes.",
                    ephemeral=True,
                )

            # Guard: ya estás en cola (EN o JP)
            for cid in ALLOWED_JOIN_CHANNELS:
                if member.id in self.queues.get(cid, set()):
                    return await interaction.followup.send(
                        "❌ You are already in the queue.", ephemeral=True
                    )

            # Lock-down: durante PREP se bloquea si ya se lockearon rooms para el slot
            # o si la queue está completa exacta en múltiplos de 5.
            now = self._now_lima()
            if await self._is_join_locked(guild, ch.id, now):
                unlock_dt = self._lock_unlock_dt_for_current_prep(now)
                if unlock_dt is not None:
                    unlock_ts = int(unlock_dt.timestamp())
                    mmss = self._format_mmss_until(unlock_dt, now)
                    return await interaction.followup.send(
                        f"⛔ Queue is locked in for this slot. Unlocks at <t:{unlock_ts}:T> "
                        f"(<t:{unlock_ts}:R>) — **{mmss}** left.",
                        ephemeral=True,
                    )
                return await interaction.followup.send(
                    "⛔ Queue is locked in for this slot.", ephemeral=True
                )

            queue_was_empty = len(self.queues.get(ch.id, set())) == 0

            # Siempre encola (no se intenta meter a rooms “unfilled”)
            self.queues.setdefault(ch.id, set()).add(member.id)
            self._queue_counter += 1
            self.queue_join_order[ch.id][member.id] = self._queue_counter

            # Si entra durante la ventana PREP de 5 minutos y era el primero del queue,
            # hacer ping al tag correspondiente: enTags para #join, jpTags para #jp-join.
            if queue_was_empty and self._in_prep_window(now) is not None:
                first_prep_ping_role_id = PING_ROLES_BY_JOIN_CHANNEL.get(ch.id)

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
        await interaction.followup.send(
            f"Queued. Future room **{room_no}** ({pos_in_room}/5). "
            f"{'Room complete.' if need == 0 else f'Need {need} more to complete the next room.'}\n"
            f"Next slot: <t:{slot_ts}:T> (<t:{slot_ts}:R>).",
            ephemeral=True,
        )

        # Mensaje público en #join / #jp-join.
        # Si fue el primer jugador dentro de la ventana PREP, se hace ping al tag del server.
        try:
            qcount = len(self.queues.get(ch.id, set()))
            ping_part = (
                f"<@&{first_prep_ping_role_id}> "
                if first_prep_ping_role_id is not None
                else ""
            )

            await ch.send(
                f"{ping_part}({member.mention}) Joined the queue (`{qcount}`)",
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=first_prep_ping_role_id is not None,
                    everyone=False,
                ),
            )
        except Exception:
            pass


    @app_commands.command(
        name="d",
        description="Drop queue before lockdown. Locked rooms cannot be left.",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def leave_queue(self, interaction: discord.Interaction):
        ch = interaction.channel
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Guild not found.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        member: discord.Member = interaction.user  # type: ignore

        # ------------------------------------------------------------
        # CASO A: /d dentro de un ROOM THREAD -> salir de la sala actual
        # ------------------------------------------------------------
        if isinstance(ch, discord.Thread):
            async with self._join_lock:
                rid, info = self._find_room_by_thread_id(ch.id)
                if not info:
                    return await interaction.followup.send(
                        "ℹ️ This thread is not an active room.", ephemeral=True
                    )

                player_ids = {
                    int(getattr(p, "id", 0) or 0)
                    for p in (info.get("players", []) or [])
                }
                if int(member.id) not in player_ids:
                    return await interaction.followup.send(
                        "ℹ️ You are not listed as a player in this room.", ephemeral=True
                    )

                # Lockdown real:
                # Después de que el bot creó la room/thread y mandó
                # "You are locked in for this slot", nadie puede salirse con /d.
                # Esto mantiene la room como 5/5 y el submit seguirá pidiendo 5 líneas.
                if self._room_is_locked_for_player_exit(info):
                    return await interaction.followup.send(
                        "❌ This room is already locked in. You cannot leave after lockdown; "
                        "you stay listed until submit.",
                        ephemeral=True,
                    )

                # Remover del room + del thread solo si todavía NO estaba locked.
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

            return await interaction.followup.send("Removed from room.", ephemeral=True)

        # ------------------------------------------------------------
        # CASO B: /d en #join / #jp-join -> salir SOLO de la queue
        # ------------------------------------------------------------
        if not isinstance(ch, discord.TextChannel) or ch.id not in ALLOWED_JOIN_CHANNELS:
            return await interaction.followup.send(
                "This command only drops queue before lockdown. Locked rooms cannot be left.",
                ephemeral=True,
            )

        async with self._join_lock:
            active_rid = self._find_player_room_id(member)
            if active_rid is not None:
                active_info = self.rooms.get(int(active_rid))
                if self._room_is_locked_for_player_exit(active_info):
                    return await interaction.followup.send(
                        "❌ You are already locked in a room. You cannot drop after lockdown; "
                        "you stay listed until submit.",
                        ephemeral=True,
                    )

            q = self.queues.get(ch.id, set())
            if member.id not in q:
                return await interaction.followup.send(
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

        return await interaction.followup.send("Removed from queue.", ephemeral=True)


    # =========================
    # Season title rewards
    # =========================
    SEASON_TITLE_RANKS = ("Star", "Divine Star", "Legend")
    SEASON_TITLE_EXACT_PLACE_LIMIT = 10
    SEASON_TITLE_TOP_CUTOFFS = (20, 30, 40, 50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000)

    @staticmethod
    def _season_title_slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")

    @staticmethod
    def _season_title_place_suffix(place: int) -> str:
        place = int(place or 0)
        if 10 <= (place % 100) <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(place % 10, "th")
        return f"{place}{suffix}"

    @staticmethod
    def _is_test_season_name(season_name: str | None) -> bool:
        raw = str(season_name or "").strip().lower()
        compact = re.sub(r"[\s_-]+", "", raw)
        if not compact:
            return False
        if "alphatest" in compact or "betatest" in compact or "testseason" in compact:
            return True
        if compact in {"preseason", "pre", "current", "seasonpreseason"}:
            return True
        m = re.search(r"season\s*(\d+)", raw, flags=re.I)
        if m:
            return int(m.group(1)) < 2
        return False

    @classmethod
    def _season_title_label_for_place(cls, season_name: str, place: int, *, current: bool = False) -> str:
        prefix = "CURRENT " if current else ""
        base = f"{prefix}{season_name} {cls._season_title_place_suffix(place)} Place".strip()
        base = re.sub(r"\s+", " ", base)
        if cls._is_test_season_name(season_name) and not base.upper().startswith("TEST "):
            base = f"TEST {base}"
        return base

    @classmethod
    def _season_title_label_for_top_cutoff(cls, season_name: str, cutoff: int, *, current: bool = False) -> str:
        prefix = "CURRENT " if current else ""
        base = f"{prefix}{season_name} TOP {int(cutoff)}".strip()
        base = re.sub(r"\s+", " ", base)
        if cls._is_test_season_name(season_name) and not base.upper().startswith("TEST "):
            base = f"TEST {base}"
        return base

    @classmethod
    def _season_title_top_cutoff_for_place(cls, place: int) -> int:
        place = max(1, int(place or 0))
        for cutoff in cls.SEASON_TITLE_TOP_CUTOFFS:
            if place <= int(cutoff):
                return int(cutoff)
        return int(cls.SEASON_TITLE_TOP_CUTOFFS[-1])

    @classmethod
    def _season_title_label_for_rank(cls, season_name: str, rank: str) -> str:
        base = f"{season_name} {rank}".strip()
        base = re.sub(r"\s+", " ", base)
        if cls._is_test_season_name(season_name) and not base.upper().startswith("TEST "):
            base = f"TEST {base}"
        return base

    @staticmethod
    def _season_title_style_for_place(place: int) -> str:
        p = int(place or 0)
        if p == 1:
            return "gold"
        if p == 2:
            return "silver"
        if p == 3:
            return "bronze"
        if p == 4:
            return "place-4"
        if p == 5:
            return "place-5"
        if 6 <= p <= 10:
            return "place-6-10"
        return "top-white"

    @staticmethod
    def _season_title_style_for_rank(rank: str) -> str:
        key = str(rank or "").replace(" ", "")
        if key == "DivineStar":
            return "rank-DivineStar"
        if key == "Legend":
            return "rank-Legend"
        return "rank-Star"

    @staticmethod
    def _season_rank_from_peak_mmr(peak_mmr: int | None) -> str | None:
        mmr = int(peak_mmr or 0)
        if mmr >= 1000:
            return "Legend"
        if mmr >= 950:
            return "Divine Star"
        if mmr >= 900:
            return "Star"
        return None

    async def _ensure_player_title_inventory_schema(self, conn) -> None:
        """
        Inventario permanente de rewards/titles.
        Nota: no reutilizamos public.player_titles porque versiones previas lo usaban
        como tabla legacy de selección de un solo title por user_id.
        """
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS public.player_title_inventory (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                season_id INTEGER NOT NULL,
                season_name TEXT NOT NULL,
                title_key TEXT NOT NULL,
                title_label TEXT NOT NULL,
                title_type TEXT NOT NULL,
                place INTEGER,
                rank TEXT,
                style_key TEXT NOT NULL,
                is_test_season BOOLEAN NOT NULL DEFAULT false,
                earned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE(user_id, title_key)
            );
            CREATE INDEX IF NOT EXISTS idx_player_title_inventory_user
              ON public.player_title_inventory(user_id);
            CREATE INDEX IF NOT EXISTS idx_player_title_inventory_season
              ON public.player_title_inventory(season_id);
            ALTER TABLE public.players
              ADD COLUMN IF NOT EXISTS selected_title_key TEXT;
            """
        )

    async def _grant_finished_season_titles(self, conn, season_id: int, season_name: str) -> dict:
        """
        Otorga rewards de la season terminada de forma idempotente:
        - Puestos exactos Top 1 al Top 10.
        - Cutoff titles desde Top 11: TOP 20/30/40/50, luego TOP 100/200/.../1000.
        - Peak rank alcanzado: Star / Divine Star / Legend.

        Es seguro llamarlo desde /seasonreset y desde /migrate_players_to_newseason.
        ON CONFLICT evita duplicados si se ejecuta más de una vez.
        """
        await self._ensure_player_title_inventory_schema(conn)

        season_id = int(season_id)
        season_name = str(season_name or f"Season {season_id}").strip() or f"Season {season_id}"
        is_test = self._is_test_season_name(season_name)

        place_payload: list[tuple] = []
        top_payload: list[tuple] = []
        rank_payload: list[tuple] = []
        current_to_final_selected_keys: list[tuple[int, str, str]] = []

        try:
            top_rows = await conn.fetch(
                """
                WITH played AS (
                    SELECT DISTINCT user_id
                    FROM public.match_history
                    WHERE season_id = $1
                ), tied AS (
                    SELECT
                        sp.user_id,
                        MIN(mh.played_at) AS first_reached_current_mmr
                    FROM public.seasonplayers sp
                    LEFT JOIN public.match_history mh
                      ON mh.season_id = sp.season_id
                     AND mh.user_id = sp.user_id
                     AND mh.mmr_after = sp.mmr
                    WHERE sp.season_id = $1
                    GROUP BY sp.user_id
                ), ranked AS (
                    SELECT
                        sp.user_id,
                        sp.mmr,
                        sp.role,
                        ROW_NUMBER() OVER (
                            ORDER BY sp.mmr DESC, tied.first_reached_current_mmr ASC NULLS LAST, sp.user_id ASC
                        ) AS place
                    FROM public.seasonplayers sp
                    INNER JOIN played p ON p.user_id = sp.user_id
                    LEFT JOIN tied ON tied.user_id = sp.user_id
                    WHERE sp.season_id = $1
                )
                SELECT user_id, place, mmr, role
                FROM ranked
                ORDER BY place ASC
                """,
                season_id,
            )
        except Exception:
            # Fallback si match_history aún no existe o está incompleto.
            top_rows = await conn.fetch(
                """
                SELECT user_id,
                       ROW_NUMBER() OVER (ORDER BY mmr DESC, user_id ASC) AS place,
                       mmr,
                       role
                FROM public.seasonplayers
                WHERE season_id = $1
                  AND mmr > 0
                ORDER BY place ASC
                """,
                season_id,
            )

        for r in top_rows:
            uid = int(r["user_id"])
            place = int(r["place"])
            if place > 1000:
                continue

            if place <= self.SEASON_TITLE_EXACT_PLACE_LIMIT:
                title_key = f"season:{season_id}:place:{place}"
                current_key = f"current:{season_id}:place:{place}"
                title_label = self._season_title_label_for_place(season_name, place)
                current_to_final_selected_keys.append((uid, current_key, title_key))
                place_payload.append(
                    (
                        uid,
                        season_id,
                        season_name,
                        title_key,
                        title_label,
                        "place",
                        place,
                        None,
                        self._season_title_style_for_place(place),
                        is_test,
                    )
                )
                continue

            cutoff = self._season_title_top_cutoff_for_place(place)
            title_key = f"season:{season_id}:top:{cutoff}"
            current_key = f"current:{season_id}:top:{cutoff}"
            title_label = self._season_title_label_for_top_cutoff(season_name, cutoff)
            current_to_final_selected_keys.append((uid, current_key, title_key))
            top_payload.append(
                (
                    uid,
                    season_id,
                    season_name,
                    title_key,
                    title_label,
                    "top",
                    None,
                    f"TOP {cutoff}",
                    "top-white",
                    is_test,
                )
            )

        try:
            peak_rows = await conn.fetch(
                """
                SELECT
                    user_id,
                    MAX(
                        CASE
                            WHEN was_placement THEN mmr_after
                            ELSE GREATEST(mmr_before, mmr_after)
                        END
                    )::int AS peak_mmr
                FROM public.match_history
                WHERE season_id = $1
                GROUP BY user_id
                HAVING MAX(
                    CASE
                        WHEN was_placement THEN mmr_after
                        ELSE GREATEST(mmr_before, mmr_after)
                    END
                ) >= 900
                ORDER BY peak_mmr DESC, user_id ASC
                """,
                season_id,
            )
        except Exception:
            peak_rows = []

        for r in peak_rows:
            uid = int(r["user_id"])
            reached_rank = self._season_rank_from_peak_mmr(r["peak_mmr"])
            if not reached_rank:
                continue
            slug = self._season_title_slug(reached_rank)
            title_key = f"season:{season_id}:rank:{slug}"
            title_label = self._season_title_label_for_rank(season_name, reached_rank)
            rank_payload.append(
                (
                    uid,
                    season_id,
                    season_name,
                    title_key,
                    title_label,
                    "rank",
                    None,
                    reached_rank,
                    self._season_title_style_for_rank(reached_rank),
                    is_test,
                )
            )

        insert_sql = """
            INSERT INTO public.player_title_inventory (
                user_id, season_id, season_name, title_key, title_label,
                title_type, place, rank, style_key, is_test_season
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (user_id, title_key) DO UPDATE SET
                season_name = EXCLUDED.season_name,
                title_label = EXCLUDED.title_label,
                title_type = EXCLUDED.title_type,
                place = EXCLUDED.place,
                rank = EXCLUDED.rank,
                style_key = EXCLUDED.style_key,
                is_test_season = EXCLUDED.is_test_season
        """

        for payload in (place_payload, top_payload, rank_payload):
            if payload:
                await conn.executemany(insert_sql, payload)

        # Si el usuario tenía seleccionado un título CURRENT de esta season,
        # al cerrar la season lo convertimos al título final equivalente.
        # Ejemplo: current:3:top:50 -> season:3:top:50.
        for uid, current_key, final_key in current_to_final_selected_keys:
            await conn.execute(
                """
                UPDATE public.players
                SET selected_title_key = $3
                WHERE user_id = $1
                  AND selected_title_key = $2
                """,
                uid,
                current_key,
                final_key,
            )

        # Los títulos CURRENT son temporales. Una vez cerrada la season,
        # se eliminan del inventario y quedan los definitivos Season X ...
        await conn.execute(
            """
            DELETE FROM public.player_title_inventory
            WHERE season_id = $1
              AND title_key LIKE $2
            """,
            season_id,
            f"current:{season_id}:%",
        )

        return {
            "season_id": season_id,
            "season_name": season_name,
            "place_titles": len(place_payload),
            "top_titles": len(top_payload),
            "rank_titles": len(rank_payload),
            "total_titles": len(place_payload) + len(top_payload) + len(rank_payload),
        }

    async def _sync_current_season_top_titles(self, conn, season_id: int, season_name: str) -> dict:
        """
        Mantiene títulos temporales CURRENT de la season activa.

        Ejemplos:
        - CURRENT Season 3 1st Place
        - CURRENT Season 3 7th Place
        - CURRENT Season 3 TOP 20 / 30 / 40 / 50 / 100 / 200 / ... / 1000

        Se recalcula de forma idempotente después de cada submit. Si un jugador
        baja de cutoff o sale del Top, su título CURRENT anterior se elimina.
        Al cerrar la season, _grant_finished_season_titles convierte/elimina
        estos CURRENT y deja los títulos finales Season X ...
        """
        await self._ensure_player_title_inventory_schema(conn)

        season_id = int(season_id)
        season_name = str(season_name or f"Season {season_id}").strip() or f"Season {season_id}"
        is_test = self._is_test_season_name(season_name)

        try:
            top_rows = await conn.fetch(
                """
                WITH played AS (
                    SELECT DISTINCT user_id
                    FROM public.match_history
                    WHERE season_id = $1
                ), tied AS (
                    SELECT
                        sp.user_id,
                        MIN(mh.played_at) AS first_reached_current_mmr
                    FROM public.seasonplayers sp
                    LEFT JOIN public.match_history mh
                      ON mh.season_id = sp.season_id
                     AND mh.user_id = sp.user_id
                     AND mh.mmr_after = sp.mmr
                    WHERE sp.season_id = $1
                    GROUP BY sp.user_id
                ), ranked AS (
                    SELECT
                        sp.user_id,
                        sp.mmr,
                        sp.role,
                        ROW_NUMBER() OVER (
                            ORDER BY sp.mmr DESC, tied.first_reached_current_mmr ASC NULLS LAST, sp.user_id ASC
                        ) AS place
                    FROM public.seasonplayers sp
                    INNER JOIN played p ON p.user_id = sp.user_id
                    LEFT JOIN tied ON tied.user_id = sp.user_id
                    WHERE sp.season_id = $1
                )
                SELECT user_id, place, mmr, role
                FROM ranked
                WHERE place <= 1000
                ORDER BY place ASC
                """,
                season_id,
            )
        except Exception:
            top_rows = await conn.fetch(
                """
                SELECT user_id,
                       ROW_NUMBER() OVER (ORDER BY mmr DESC, user_id ASC) AS place,
                       mmr,
                       role
                FROM public.seasonplayers
                WHERE season_id = $1
                  AND mmr > 0
                ORDER BY place ASC
                LIMIT 1000
                """,
                season_id,
            )

        current_payload: list[tuple] = []
        current_keys: list[str] = []

        for r in top_rows:
            uid = int(r["user_id"])
            place = int(r["place"])

            if place <= self.SEASON_TITLE_EXACT_PLACE_LIMIT:
                title_key = f"current:{season_id}:place:{place}"
                title_label = self._season_title_label_for_place(season_name, place, current=True)
                current_payload.append(
                    (
                        uid,
                        season_id,
                        season_name,
                        title_key,
                        title_label,
                        "current_place",
                        place,
                        None,
                        self._season_title_style_for_place(place),
                        is_test,
                    )
                )
                current_keys.append(title_key)
                continue

            cutoff = self._season_title_top_cutoff_for_place(place)
            title_key = f"current:{season_id}:top:{cutoff}"
            title_label = self._season_title_label_for_top_cutoff(season_name, cutoff, current=True)
            current_payload.append(
                (
                    uid,
                    season_id,
                    season_name,
                    title_key,
                    title_label,
                    "current_top",
                    None,
                    f"TOP {cutoff}",
                    "top-white",
                    is_test,
                )
            )
            current_keys.append(title_key)

        keep_keys = current_keys or ["__none__"]
        current_pattern = f"current:{season_id}:%"

        # Limpia selección si apuntaba a un CURRENT que ya no corresponde.
        await conn.execute(
            """
            UPDATE public.players
            SET selected_title_key = NULL
            WHERE selected_title_key LIKE $1
              AND selected_title_key <> ALL($2::text[])
            """,
            current_pattern,
            keep_keys,
        )

        # Elimina CURRENT obsoletos del inventario.
        await conn.execute(
            """
            DELETE FROM public.player_title_inventory
            WHERE season_id = $1
              AND title_key LIKE $2
              AND title_key <> ALL($3::text[])
            """,
            season_id,
            current_pattern,
            keep_keys,
        )

        insert_sql = """
            INSERT INTO public.player_title_inventory (
                user_id, season_id, season_name, title_key, title_label,
                title_type, place, rank, style_key, is_test_season
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (user_id, title_key) DO UPDATE SET
                season_name = EXCLUDED.season_name,
                title_label = EXCLUDED.title_label,
                title_type = EXCLUDED.title_type,
                place = EXCLUDED.place,
                rank = EXCLUDED.rank,
                style_key = EXCLUDED.style_key,
                is_test_season = EXCLUDED.is_test_season
        """
        if current_payload:
            await conn.executemany(insert_sql, current_payload)

        return {
            "season_id": season_id,
            "season_name": season_name,
            "current_titles": len(current_payload),
        }


    @app_commands.command(
        name="migrate_players_to_newseason",
        description="Migrates players to the active season."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        announce="Announce the migration in #historico.",
        wipe_current="Delete existing rows for current season first."
    )
    async def migrate_players_to_newseason(
        self,
        interaction: discord.Interaction,
        announce: bool = True,
        wipe_current: bool = False,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Guild not found.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if self.db_pool is None:
            return await interaction.followup.send(
                "❌ Database pool not initialized. Restart the bot and try again.",
                ephemeral=True,
            )

        current_season = await self.get_current_season()
        if not current_season:
            return await interaction.followup.send(
                "❌ No hay una season activa en `seasons`.",
                ephemeral=True,
            )

        cur_id = int(current_season["id"])
        cur_name = str(current_season["name"])

        prev_season = await self._get_previous_season(cur_id)
        if not prev_season:
            return await interaction.followup.send(
                "❌ No encontré una season previa para migrar.",
                ephemeral=True,
            )

        prev_id = int(prev_season["id"])
        prev_name = str(prev_season["name"])

        placement_role = guild.get_role(PLACEMENT_ROLE_ID)
        season_placement_role = guild.get_role(SEASON_PLACEMENT_ROLE_ID)

        if placement_role is None:
            return await interaction.followup.send(
                f"❌ No encuentro rol Placement (ID={PLACEMENT_ROLE_ID}).",
                ephemeral=True,
            )

        if season_placement_role is None:
            return await interaction.followup.send(
                f"❌ No encuentro rol Season Placement (ID={SEASON_PLACEMENT_ROLE_ID}).",
                ephemeral=True,
            )

        # Solo jugadores registrados que sigan estando en el servidor
        guild_member_ids = {m.id for m in guild.members if not m.bot}

        title_summary = {"place_titles": 0, "top_titles": 0, "rank_titles": 0, "total_titles": 0}

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                # Fallback fundamental: si la nueva season ya existe,
                # otorgamos rewards/titles de la season previa antes de migrar.
                title_summary = await self._grant_finished_season_titles(conn, prev_id, prev_name)

                registered_rows = await conn.fetch("SELECT user_id FROM players")
                registered_ids = {int(r["user_id"]) for r in registered_rows}

                target_ids = registered_ids & guild_member_ids
                if not target_ids:
                    return await interaction.followup.send(
                        "⚠️ No encontré jugadores registrados que sigan en el servidor.",
                        ephemeral=True,
                    )

                if wipe_current:
                    await conn.execute(
                        """
                        DELETE FROM seasonplayers
                        WHERE season_id = $1
                          AND user_id = ANY($2::bigint[])
                        """,
                        cur_id,
                        list(target_ids),
                    )

                # Returning players = sí jugaron / sí tuvieron mmr real en la season anterior
                returning_rows = await conn.fetch(
                    """
                    SELECT DISTINCT sp.user_id
                    FROM seasonplayers sp
                    WHERE sp.season_id = $1
                      AND (
                            sp.mmr > 0
                            OR EXISTS (
                                SELECT 1
                                FROM match_history mh
                                WHERE mh.season_id = sp.season_id
                                  AND mh.user_id = sp.user_id
                            )
                      )
                    """,
                    prev_id,
                )

                returning_ids = {int(r["user_id"]) for r in returning_rows} & target_ids
                placement_ids = target_ids - returning_ids

                payload = []
                player_updates = []

                for uid in sorted(target_ids):
                    if uid in returning_ids:
                        prev_mmr = await conn.fetchval(
                            """
                            SELECT mmr
                            FROM seasonplayers
                            WHERE season_id = $1 AND user_id = $2
                            """,
                            prev_id,
                            uid,
                        ) or 0

                        seeded_mmr = self._clamp_mmr(int(prev_mmr) - SEASON_RESET_PENALTY)
                        seeded_role = get_rank_from_mmr(seeded_mmr)
                    else:
                        seeded_mmr = 0
                        seeded_role = "Placement"

                    is_sp_seed = uid in returning_ids
                    payload.append((cur_id, uid, seeded_mmr, seeded_role, is_sp_seed, seeded_mmr if is_sp_seed else None))
                    player_updates.append((uid, seeded_mmr, seeded_role))

                # Upsert seasonplayers de la season activa
                CHUNK = 500
                for i in range(0, len(payload), CHUNK):
                    await conn.executemany(
                        """
                        INSERT INTO seasonplayers (season_id, user_id, mmr, role, is_season_placement, starting_mmr)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (season_id, user_id)
                        DO UPDATE SET
                            mmr = EXCLUDED.mmr,
                            role = EXCLUDED.role,
                            is_season_placement = EXCLUDED.is_season_placement,
                            starting_mmr = EXCLUDED.starting_mmr
                        """,
                        payload[i:i + CHUNK],
                    )

                # Coherencia visual en tabla players
                for i in range(0, len(player_updates), CHUNK):
                    await conn.executemany(
                        """
                        UPDATE players
                        SET mmr = $2, role = $3
                        WHERE user_id = $1
                        """,
                        player_updates[i:i + CHUNK],
                    )

        # Reasignar roles en Discord
        rank_role_ids = set(RANK_ROLE_IDS.values()) | {SEASON_PLACEMENT_ROLE_ID}

        me = guild.get_member(self.bot.user.id) if self.bot.user else None
        can_manage_nicks = bool(me and me.guild_permissions.manage_nicknames)

        edited = 0
        failed = 0

        for uid in sorted(target_ids):
            member = guild.get_member(uid)
            if not member or member.bot:
                continue

            keep = [r for r in member.roles if r.id not in rank_role_ids]

            # Returning players -> Season Placement
            # Nuevos / sin historial -> Placement normal
            target_role = season_placement_role if uid in returning_ids else placement_role
            new_roles = list(dict.fromkeys(keep + [target_role]))

            try:
                new_nick = None
                if can_manage_nicks and member.nick:
                    new_nick = re.sub(r"\s*\[[^\]]+\]\s*$", "", member.nick).strip() or None

                await member.edit(
                    roles=new_roles,
                    nick=new_nick if can_manage_nicks else discord.utils.MISSING,
                    reason=f"Migrate players to active season -> {cur_name}",
                )
                edited += 1
            except Exception:
                failed += 1

            if (edited + failed) % 10 == 0:
                await asyncio.sleep(1.0)

        if announce:
            historico = discord.utils.get(guild.text_channels, name=self.HISTORICO_CHANNEL_NAME)
            if historico:
                try:
                    await historico.send(
                        f"🛠️ **Season migration applied**\n"
                        f"📦 **Previous season**: **{prev_name}** (ID {prev_id})\n"
                        f"🆕 **Active season**: **{cur_name}** (ID {cur_id})\n"
                        f"🔁 **Season Placement**: {len(returning_ids)} returning players\n"
                        f"🆕 **Placement**: {len(placement_ids)} new / sin historial\n"
                        f"👥 **Migrated members in server**: {len(target_ids)}\n"
                        f"✅ **Roles edited**: {edited}\n"
                        f"⚠️ **Role edit failed**: {failed}"
                    )
                except Exception:
                    pass

        await interaction.followup.send(
            f"✅ Migration complete.\n"
            f"**Previous season:** {prev_name} (ID {prev_id})\n"
            f"**Active season:** {cur_name} (ID {cur_id})\n"
            f"**Migrated registered members in server:** {len(target_ids)}\n"
            f"**Season Placement:** {len(returning_ids)}\n"
            f"**Placement:** {len(placement_ids)}\n"
            f"**Roles edited:** {edited}\n"
            f"**Role edit failed:** {failed}",
            ephemeral=True,
        )



    async def _send_stats_image(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        season_id: int,
        season_name: str,
    ):
        """
        Genera y envía la imagen de /stats y /stats_user.
        No cambia MMR, DB, rooms ni submit. Solo usa la data ya calculada por _fetch_user_stats.
        """
        data = await self._fetch_user_stats(interaction.guild, user, season_id)

        if not data:
            player_name = await self.resolve_member_name(interaction.guild, user.id)
            return await interaction.followup.send(
                f"{player_name} doesn't have register in **Season {season_name}**.",
                ephemeral=True,
            )

        try:
            avatar_asset = user.display_avatar.with_size(256)
            avatar_bytes = await avatar_asset.read()
        except Exception:
            avatar_bytes = None

        image_buffer = generate_stats_image(
            player_name=user.display_name,
            avatar_bytes=avatar_bytes,
            rank=str(data["role"]),
            position=int(data["pos"]),
            total_players=int(data["total"]),
            matches_played=int(data["matches_played"]),
            last10_wins=int(data["last10_wins"]),
            last10_losses=int(data["last10_losses"]),
            peak_mmr=int(data["peak_mmr"]),
            current_mmr=int(data["mmr"]),
            season_name=str(season_name),
            last10_mmr_delta=int(data.get("last10_mmr_delta", 0)),
            country_code=str(data.get("country_code") or ""),
            is_season_placement=bool(data.get("is_season_placement", False)),
        )

        file = discord.File(
            image_buffer,
            filename=f"stats_{user.id}_{season_id}.png",
        )

        await interaction.followup.send(file=file)

    @app_commands.command(
        name="stats",
        description="Shows your position, mmr and rank in the selected season",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(season="Select a season.")
    @app_commands.choices(season=SEASON_CHOICES)
    async def stats(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)

        if not season_id:
            return await interaction.followup.send(
                "⚠️ Season not found.",
                ephemeral=True,
            )

        await self._send_stats_image(
            interaction=interaction,
            user=interaction.user,
            season_id=int(season_id),
            season_name=str(season_name),
        )

    @app_commands.command(
        name="searchresult", description="Show a past results table by its global ID"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        result_id="Result ID shown under the season name in the results embed"
    )
    async def searchresult(self, interaction: discord.Interaction, result_id: int):
        await interaction.response.defer()

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
            return await interaction.followup.send(
                f"⚠️ No result found with ID `{result_id}`.", ephemeral=True
            )

        channel = await self._resolve_channel(
            row["channel_id"], fallback=interaction.channel
        )
        try:
            msg = await channel.fetch_message(row["message_id"])
        except discord.NotFound:
            return await interaction.followup.send(
                "⚠️ The original result message could not be found (maybe deleted).",
                ephemeral=True,
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "⚠️ I don't have permissions to view the channel of that result.",
                ephemeral=True,
            )

        content = (
            f"Result **#{row['result_id']}** — **{row['season_name']}**\n"
            f"[Jump to original message]({msg.jump_url})"
        )

        if msg.embeds:
            await interaction.followup.send(content, embed=msg.embeds[0])
        else:
            await interaction.followup.send(content)

    @app_commands.command(
        name="stats_user",
        description="Shows another player's stats for the selected season",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        user="Player",
        season="Select a season.",
    )
    @app_commands.choices(season=SEASON_CHOICES)
    async def stats_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        season: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)

        if not season_id:
            return await interaction.followup.send(
                "⚠️ No encontré la temporada seleccionada.",
                ephemeral=True,
            )

        await self._send_stats_image(
            interaction=interaction,
            user=user,
            season_id=int(season_id),
            season_name=str(season_name),
        )

    @app_commands.command(
        name="cutoffs",
        description="Shows the MMR cutoff table for the selected season",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(season="Select a season.")
    @app_commands.choices(season=SEASON_CHOICES)
    
    async def cutoffs(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.followup.send(
                "⚠️ No encontré la temporada seleccionada.", ephemeral=True
            )

        tiers = (1, 10, 20, 30, 40, 50, 100, 200, 300, 400, 500, 1000)
        data = await self._build_cutoffs_data(int(season_id), tiers)
        board = self._format_cutoffs_board(str(season_name), data)
        await interaction.followup.send(board)

    @staticmethod
    def _inline_code(value) -> str:
        """Discord inline-code seguro para valores cortos."""
        s = str(value if value is not None else "N/A")
        s = s.replace("`", "´")
        return f"`{s}`"

    @staticmethod
    def _season_sort_key(row) -> tuple[int, int, str]:
        """
        Orden visual del historial:
        - Season activa primero.
        - Luego por id descendente.
        """
        active = 1 if bool(row.get("active", False)) else 0
        sid = int(row.get("season_id") or 0)
        return (active, sid, str(row.get("season_name") or ""))

    @staticmethod
    def _is_test_season_name(season_name: str | None) -> bool:
        """
        TEST Seasons = Season 1 y anteriores/pre-test.
        Inserta el separador entre Season 2 y Season 1 en All Seasons.
        """
        s = str(season_name or "").strip().lower()
        if not s:
            return False

        m = re.search(r"\bseason\s*(\d+)\b", s)
        if m:
            return int(m.group(1)) <= 1

        return s in {
            "pre season",
            "preseason",
            "beta test season",
            "alpha test season",
        }

    _MMR_EMBED_RANK_COLORS = {
        "Placement": 0x7F8C8D,
        "Season Placement": 0xF39C12,
        "Iron": 0x707B7C,
        "Iron ⬆": 0x85929E,
        "Bronze": 0xA97142,
        "Bronze ⬆": 0xC27C4A,
        "Silver": 0xC0C7D1,
        "Silver ⬆": 0xD6DCE5,
        "Gold": 0xD4AF37,
        "Gold ⬆": 0xE6C34A,
        "Platinum": 0x48C9B0,
        "Platinum ⬆": 0x5DE2C8,
        "Diamond": 0x5B8CFF,
        "Diamond ⬆": 0x7AA5FF,
        "Crystal": 0x8E6BFF,
        "Crystal ⬆": 0xA98BFF,
        "Master": 0xB06CFF,
        "Master ⬆": 0xC58BFF,
        "Champion": 0xFF6B6B,
        "Grand Champion": 0xFF8A65,
        "Star": 0xFF66C4,
        "Divine Star": 0xFF84E1,
        "Legend": 0xFFB347,
    }

    def _mmr_card_user_name(self, user: discord.Member) -> str:
        return (
            getattr(user, "display_name", None)
            or getattr(user, "global_name", None)
            or getattr(user, "name", None)
            or f"User {getattr(user, 'id', 'unknown')}"
        )

    def _mmr_card_visual_rank(self, row: dict) -> str:
        mmr_val = int(row.get("mmr") or 0)
        role = str(row.get("role") or "").strip()
        matches_played = int(row.get("matches_played") or 0)

        is_season_placement = bool(
            matches_played == 0
            and (
                bool(row.get("is_season_placement", False))
                or role == "Season Placement"
            )
        )

        if is_season_placement:
            if mmr_val > 0:
                return get_rank_from_mmr(mmr_val)
            return "Season Placement"

        if role == "Placement" and mmr_val <= 0:
            return "Placement"

        if role:
            return role

        if mmr_val > 0:
            return get_rank_from_mmr(mmr_val)

        return "Placement"

    def _mmr_embed_color(self, row: dict) -> int:
        visual_rank = self._mmr_card_visual_rank(row)
        return int(
            self._MMR_EMBED_RANK_COLORS.get(
                visual_rank,
                self._MMR_EMBED_RANK_COLORS["Placement"],
            )
        )

    def _mmr_card_value_label_role(self, row: dict) -> tuple[str, str, str]:
        season_name = str(row.get("season_name") or "Current")
        mmr_val = int(row.get("mmr") or 0)
        starting_raw = row.get("starting_mmr")
        starting_mmr = int(starting_raw) if starting_raw is not None else mmr_val
        role = str(row.get("role") or get_rank_from_mmr(mmr_val)).strip()
        matches_played = int(row.get("matches_played") or 0)

        is_season_placement = bool(
            matches_played == 0
            and (
                bool(row.get("is_season_placement", False))
                or role == "Season Placement"
            )
        )

        if is_season_placement:
            return (
                str(starting_mmr),
                "Starting MMR",
                "Season Placement",
            )

        if role == "Placement" and mmr_val <= 0:
            return (
                "Placement",
                "Status",
                "Placement",
            )

        display_role = role or get_rank_from_mmr(mmr_val)
        return (
            str(mmr_val),
            "MMR",
            display_role,
        )

    def _mmr_footer_text(self, interaction: discord.Interaction | None = None) -> str:
        now = self._now_lima()
        return f"PJSK Lounge • creado hoy a las {now:%H:%M}"

    def _build_mmr_embed(
        self,
        *,
        interaction: discord.Interaction | None,
        user: discord.Member,
        row: dict,
        include_matches: bool = False,
    ) -> discord.Embed:
        season_name = str(row.get("season_name") or "Current")
        user_name = self._mmr_card_user_name(user)
        value_text, value_label, role_text = self._mmr_card_value_label_role(row)
        matches_played = int(row.get("matches_played") or 0)

        embed = discord.Embed(
            title=f"PJSK LOUNGE - {season_name}",
            color=self._mmr_embed_color(row),
        )

        # Se usa un field para imitar el bloque visual tipo input de Discord.
        embed.add_field(
            name=user_name,
            value=f"```{value_text}```",
            inline=False,
        )

        description_lines = [f"**{value_label}:** `{value_text}`", f"**Rank:** `{role_text}`"]
        if include_matches:
            description_lines.append(f"**Matches:** `{matches_played}`")
        embed.description = "\n".join(description_lines)

        icon_url = None
        guild = getattr(interaction, "guild", None) if interaction else None
        try:
            if guild and guild.icon:
                icon_url = guild.icon.url
        except Exception:
            icon_url = None

        embed.set_footer(text=self._mmr_footer_text(interaction), icon_url=icon_url)
        return embed

    async def _fetch_mmr_rows_for_user_all_seasons(self, user_id: int) -> list[dict]:
        """
        Devuelve solo seasons donde el usuario tiene registro real:
        seasonplayers o match_history. Así un usuario nuevo en Season 3
        no aparece artificialmente como Placement en seasons pasadas.
        """
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH mh AS (
                    SELECT season_id, user_id, COUNT(*)::int AS matches_played
                    FROM match_history
                    WHERE user_id = $1
                    GROUP BY season_id, user_id
                )
                SELECT
                    s.id AS season_id,
                    s.name AS season_name,
                    COALESCE(s.active, false) AS active,
                    sp.user_id AS player_user_id,
                    COALESCE(sp.mmr, 0) AS mmr,
                    COALESCE(sp.role, 'Placement') AS role,
                    COALESCE(sp.is_season_placement, false) AS is_season_placement,
                    sp.starting_mmr,
                    COALESCE(mh.matches_played, 0) AS matches_played
                FROM seasons s
                LEFT JOIN seasonplayers sp
                  ON sp.season_id = s.id
                 AND sp.user_id = $1
                LEFT JOIN mh
                  ON mh.season_id = s.id
                 AND mh.user_id = $1
                WHERE sp.user_id IS NOT NULL
                   OR COALESCE(mh.matches_played, 0) > 0
                ORDER BY COALESCE(s.active, false) DESC,
                         s.startdate DESC NULLS LAST,
                         s.id DESC
                """,
                int(user_id),
            )

        out = [dict(r) for r in rows]
        out.sort(key=self._season_sort_key, reverse=True)
        return out

    async def _fetch_mmr_row_for_user_season(self, user_id: int, season_id: int) -> dict | None:
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    s.id AS season_id,
                    s.name AS season_name,
                    COALESCE(s.active, false) AS active,
                    sp.user_id AS player_user_id,
                    COALESCE(sp.mmr, 0) AS mmr,
                    COALESCE(sp.role, 'Placement') AS role,
                    COALESCE(sp.is_season_placement, false) AS is_season_placement,
                    sp.starting_mmr,
                    COALESCE(mh.matches_played, 0) AS matches_played
                FROM seasons s
                LEFT JOIN seasonplayers sp
                  ON sp.season_id = s.id
                 AND sp.user_id = $1
                LEFT JOIN (
                    SELECT season_id, user_id, COUNT(*)::int AS matches_played
                    FROM match_history
                    WHERE user_id = $1
                    GROUP BY season_id, user_id
                ) mh ON mh.season_id = s.id AND mh.user_id = $1
                WHERE s.id = $2
                LIMIT 1
                """,
                int(user_id),
                int(season_id),
            )

        if not row:
            return None

        out = dict(row)
        has_real_record = bool(out.get("player_user_id") is not None) or int(out.get("matches_played") or 0) > 0
        if not has_real_record:
            return None

        return out

    async def _apply_current_season_placement_override(
        self,
        user: discord.Member,
        row: dict,
        season_id: int,
    ) -> dict:
        """
        Si la DB todavía no marcó is_season_placement pero Discord sí tiene el rol,
        se respeta el estado visual para la season activa.
        """
        is_current_season = await self._is_current_season_id(int(season_id))
        if is_current_season and self._has_season_placement_role(user):
            row["is_season_placement"] = True
            row["role"] = "Season Placement"
            if row.get("starting_mmr") is None:
                row["starting_mmr"] = int(row.get("mmr") or 0)
        return row

    async def _send_mmr_all_seasons(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        name = self._mmr_card_user_name(user)
        rows = await self._fetch_mmr_rows_for_user_all_seasons(int(user.id))

        current = await self.get_current_season()
        current_id = int(current["id"]) if current else None
        if current_id:
            for row in rows:
                if int(row.get("season_id") or 0) == current_id:
                    await self._apply_current_season_placement_override(user, row, current_id)
                    break

        if not rows:
            return await interaction.followup.send(
                f"**MMR History — {name}**\n• Record: {self._inline_code('No seasons found')}",
                ephemeral=True,
            )

        normal_rows: list[dict] = []
        test_rows: list[dict] = []

        for row in rows:
            season_name = str(row.get("season_name") or "")
            if self._is_test_season_name(season_name):
                test_rows.append(row)
            else:
                normal_rows.append(row)

        normal_embeds = [
            self._build_mmr_embed(interaction=interaction, user=user, row=row, include_matches=True)
            for row in normal_rows
        ]
        test_embeds = [
            self._build_mmr_embed(interaction=interaction, user=user, row=row, include_matches=True)
            for row in test_rows
        ]

        header = f"**MMR History — {name}**"
        sent_header = False

        for idx in range(0, len(normal_embeds), 10):
            await interaction.followup.send(
                content=header if not sent_header else None,
                embeds=normal_embeds[idx : idx + 10],
            )
            sent_header = True

        if test_embeds:
            await interaction.followup.send(
                content=(header + "\n**-----TEST Seasons-----**") if not sent_header else "**-----TEST Seasons-----**",
                embeds=test_embeds[:10],
            )
            sent_header = True

            for idx in range(10, len(test_embeds), 10):
                await interaction.followup.send(embeds=test_embeds[idx : idx + 10])

    async def _send_mmr_single_season(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        season_id: int,
        season_name: str,
    ):
        name = self._mmr_card_user_name(user)
        row = await self._fetch_mmr_row_for_user_season(int(user.id), int(season_id))

        if row is None:
            # Fallback limpio para usuario sin registro en esa season.
            if await self._is_current_season_id(int(season_id)) and self._is_placement_member(user):
                row = {
                    "season_id": int(season_id),
                    "season_name": str(season_name),
                    "active": True,
                    "mmr": 0,
                    "role": "Placement",
                    "is_season_placement": False,
                    "starting_mmr": None,
                    "matches_played": 0,
                }
            else:
                return await interaction.followup.send(
                    f"**MMR — {name}**\n"
                    f"• Season: {self._inline_code(season_name)}\n"
                    f"• Record: {self._inline_code('No record')}",
                    ephemeral=True,
                )

        row["season_name"] = str(season_name or row.get("season_name") or "Current")
        row = await self._apply_current_season_placement_override(user, row, int(season_id))

        embed = self._build_mmr_embed(interaction=interaction, user=user, row=row)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="mmr", description="Shows your MMR and Rank")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(season="Season to check. Use All Seasons to see your full history.")
    @app_commands.choices(season=MMR_USER_SEASON_CHOICES)
    async def mmr_self(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        season_val = season.value if season else "current"
        if str(season_val).lower().strip() in {"all", "all seasons", "todas", "todas las seasons"}:
            return await self._send_mmr_all_seasons(interaction, interaction.user)

        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.followup.send(
                "⚠️ No encontré la temporada seleccionada.",
                ephemeral=True,
            )

        await self._send_mmr_single_season(
            interaction=interaction,
            user=interaction.user,
            season_id=int(season_id),
            season_name=str(season_name),
        )

    @app_commands.command(
        name="mmr_user",
        description="Shows the user MMR and Rank",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        user="Usuario a consultar",
        season="Temporada a consultar. Usa All Seasons para ver todas.",
    )
    @app_commands.choices(season=MMR_USER_SEASON_CHOICES)
    async def mmr_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        season: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        season_val = season.value if season else "current"
        if str(season_val).lower().strip() in {"all", "all seasons", "todas", "todas las seasons"}:
            return await self._send_mmr_all_seasons(interaction, user)

        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.followup.send(
                "⚠️ No encontré la temporada seleccionada.",
                ephemeral=True,
            )

        await self._send_mmr_single_season(
            interaction=interaction,
            user=user,
            season_id=int(season_id),
            season_name=str(season_name),
        )

    @app_commands.command(
        name="top100", description="Shows the top 100 players by MMR."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.describe(
        season="Select a season."
    )
    @app_commands.choices(season=SEASON_CHOICES)
    
    async def top100(
        self,
        interaction: discord.Interaction,
        season: app_commands.Choice[str] | None = None,
    ):
        await interaction.response.defer()

        # Resolver season (igual que en /stats, /cutoffs, etc.)
        season_val = season.value if season else None
        season_id, season_name = await self._resolve_season_id(season_val)
        if not season_id:
            return await interaction.followup.send(
                "⚠️ Season not found.", ephemeral=True
            )

        view = Top100View(self, self.db_pool, interaction, season_id, season_name)

        # Carga una sola vez. El view usa cache para paginación.
        rows = await view._get_rows()
        total = min(len(rows), 100)
        max_page = (total - 1) // view.per_page if total else 0
        view.prev_page.disabled = True
        view.next_page.disabled = 0 == max_page

        embed = await view.build_embed()
        await interaction.followup.send(embed=embed, view=view)

# Pon esto DENTRO de class Matchmaking(commands.Cog):


    HISTORICO_CHANNEL_NAME = "historico"

    @staticmethod
    def _guess_next_season_name(current_name: str | None) -> str:
        if not current_name:
            return "Season 1"
        m = re.search(r"(\d+)", current_name)
        if m:
            n = int(m.group(1)) + 1
            return re.sub(r"\d+", str(n), current_name, count=1)
        return f"{current_name} (New)"

    @app_commands.command(
        name="seasonreset",
        description="Reinicia la temporada y limpia estadísticas."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        name="Nombre de la nueva season (opcional). Si se omite, se autogenera.",
        announce="Si true, anuncia el cierre en #historico (si existe)."
    )
    async def seasonreset(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        announce: bool = True,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Guild not found.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                cur = await conn.fetchrow(
                    """
                    SELECT id, name
                    FROM seasons
                    WHERE active = true
                    ORDER BY startdate DESC
                    LIMIT 1
                    """
                )
                if not cur:
                    return await interaction.followup.send(
                        "❌ No hay una season activa en `seasons`.", ephemeral=True
                    )

                cur_id = int(cur["id"])
                cur_name = str(cur["name"])

                # Otorgar rewards/titles de la season que termina ANTES de cerrarla.
                # Es idempotente: si se vuelve a ejecutar, no duplica títulos.
                title_summary = await self._grant_finished_season_titles(conn, cur_id, cur_name)

                # cerrar season actual
                await conn.execute(
                    "UPDATE seasons SET active=false, enddate=now() WHERE id=$1",
                    cur_id,
                )

                # crear nueva season
                new_name = (name or self._guess_next_season_name(cur_name)).strip()
                new_row = await conn.fetchrow(
                    """
                    INSERT INTO seasons (name, active, startdate)
                    VALUES ($1, true, now())
                    RETURNING id
                    """,
                    new_name,
                )
                new_id = int(new_row["id"])

                # players registrados
                registered_rows = await conn.fetch("SELECT user_id FROM players")
                registered_ids = {int(r["user_id"]) for r in registered_rows}

                # returning players = sí jugaron / sí tuvieron mmr real en la season anterior
                returning_rows = await conn.fetch(
                    """
                    SELECT DISTINCT sp.user_id
                    FROM seasonplayers sp
                    WHERE sp.season_id = $1
                    AND (
                            sp.mmr > 0
                            OR EXISTS (
                                SELECT 1
                                FROM match_history mh
                                WHERE mh.season_id = sp.season_id
                                AND mh.user_id = sp.user_id
                            )
                        )
                    """,
                    cur_id,
                )
                returning_ids = {int(r["user_id"]) for r in returning_rows} & registered_ids
                placement_ids = registered_ids - returning_ids

                # nueva season:
                # - returning players: arrancan con prev_season_mmr - 50
                # - new players: arrancan en 0 / Placement
                if registered_ids:
                    payload = []
                
                    for uid in sorted(registered_ids):
                        if uid in returning_ids:
                            prev_mmr = await conn.fetchval(
                                """
                                SELECT mmr
                                FROM seasonplayers
                                WHERE season_id = $1 AND user_id = $2
                                """,
                                cur_id,
                                uid,
                            ) or 0
                
                            seeded_mmr = self._clamp_mmr(int(prev_mmr) - SEASON_RESET_PENALTY)
                            seeded_role = get_rank_from_mmr(seeded_mmr)
                
                            payload.append((new_id, uid, seeded_mmr, seeded_role, True, seeded_mmr))
                        else:
                            payload.append((new_id, uid, 0, "Placement", False, None))
                
                    CHUNK = 500
                    for i in range(0, len(payload), CHUNK):
                        await conn.executemany(
                            """
                            INSERT INTO seasonplayers (season_id, user_id, mmr, role, is_season_placement, starting_mmr)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            ON CONFLICT (season_id, user_id)
                            DO UPDATE SET
                                mmr = EXCLUDED.mmr,
                                role = EXCLUDED.role,
                                is_season_placement = EXCLUDED.is_season_placement,
                                starting_mmr = EXCLUDED.starting_mmr
                            """,
                            payload[i:i+CHUNK],
                        )

                # opcional: coherencia visual en tabla players
                if registered_ids:
                    for uid in sorted(registered_ids):
                        if uid in returning_ids:
                            prev_mmr = await conn.fetchval(
                                """
                                SELECT mmr
                                FROM seasonplayers
                                WHERE season_id = $1 AND user_id = $2
                                """,
                                cur_id,
                                uid,
                            ) or 0
                
                            seeded_mmr = self._clamp_mmr(int(prev_mmr) - SEASON_RESET_PENALTY)
                            seeded_role = get_rank_from_mmr(seeded_mmr)
                
                            await conn.execute(
                                """
                                UPDATE players
                                SET mmr = $2, role = $3
                                WHERE user_id = $1
                                """,
                                uid,
                                seeded_mmr,
                                seeded_role,
                            )
                        else:
                            await conn.execute(
                                """
                                UPDATE players
                                SET mmr = 0, role = 'Placement'
                                WHERE user_id = $1
                                """,
                                uid,
                            )

        placement_role = guild.get_role(PLACEMENT_ROLE_ID)
        season_placement_role = guild.get_role(SEASON_PLACEMENT_ROLE_ID)

        if placement_role is None:
            return await interaction.followup.send(
                f"❌ No encuentro rol Placement (ID={PLACEMENT_ROLE_ID}).",
                ephemeral=True,
            )

        if season_placement_role is None:
            return await interaction.followup.send(
                f"❌ No encuentro rol Season Placement (ID={SEASON_PLACEMENT_ROLE_ID}).",
                ephemeral=True,
            )

        # limpiar cualquier rank normal + season placement antes de reasignar
        rank_role_ids = set(RANK_ROLE_IDS.values()) | {SEASON_PLACEMENT_ROLE_ID}

        me = guild.get_member(self.bot.user.id) if self.bot.user else None
        can_manage_nicks = bool(me and me.guild_permissions.manage_nicknames)

        edited = 0
        for uid in sorted(registered_ids):
            member = guild.get_member(uid)
            if not member or member.bot:
                continue

            keep = [r for r in member.roles if r.id not in rank_role_ids]

            # Returning players -> Season Placement
            # New / sin historial -> Placement normal
            target_role = season_placement_role if uid in returning_ids else placement_role
            new_roles = list(dict.fromkeys(keep + [target_role]))

            try:
                new_nick = None
                if can_manage_nicks and member.nick:
                    new_nick = re.sub(r"\s*\[[^\]]+\]\s*$", "", member.nick).strip() or None

                await member.edit(
                    roles=new_roles,
                    nick=new_nick if can_manage_nicks else discord.utils.MISSING,
                    reason=f"Season reset -> {new_name}",
                )
                edited += 1
            except Exception:
                pass

            if edited % 10 == 0:
                await asyncio.sleep(1.0)

        if announce:
            historico = discord.utils.get(guild.text_channels, name=self.HISTORICO_CHANNEL_NAME)
            if historico:
                try:
                    await historico.send(
                        f"📦 **Season archived**: **{cur_name}** (ID {cur_id})\n"
                        f"🆕 **New season**: **{new_name}** (ID {new_id})\n"
                        f"🔁 **Season Placement**: {len(returning_ids)} returning players\n"
                        f"🆕 **Placement**: {len(placement_ids)} new / sin historial"
                    )
                except Exception:
                    pass

        await interaction.followup.send(
            f"✅ Season reset done.\n"
            f"- Closed: **{cur_name}** (ID {cur_id})\n"
            f"- New: **{new_name}** (ID {new_id})\n"
            f"- Season Placement: **{len(returning_ids)}**\n"
            f"- Placement normal: **{len(placement_ids)}**",
            ephemeral=True,
        )

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

    async def _get_thread_obj(self, info: dict) -> discord.Thread | None:
        """
        Devuelve el objeto discord.Thread de un room_info.
        - Primero intenta get_thread() desde el parent (cache).
        - Si no está en cache, hace fetch_channel(thread_id).
        """
        try:
            parent_id = int(info.get("join_channel_id") or 0)
            thread_id = int(info.get("thread") or 0)
            if parent_id <= 0 or thread_id <= 0:
                return None

            parent = self.bot.get_channel(parent_id)
            if isinstance(parent, discord.TextChannel):
                th = parent.get_thread(thread_id)
                if isinstance(th, discord.Thread):
                    return th

            # fallback: fetch por ID del thread
            try:
                fetched = await self.bot.fetch_channel(thread_id)
                return fetched if isinstance(fetched, discord.Thread) else None
            except Exception:
                return None
        except Exception:
            return None

    
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



    @app_commands.command(name="songban", description="Ban song by title from queue.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        title="Exact or display song title.",
        server="all, en or jp.",
        diff="Optional: easy, normal, hard, expert, master or append.",
        reason="Optional reason.",
    )
    async def songban(
        self,
        interaction: discord.Interaction,
        title: str,
        server: str = "all",
        diff: str | None = None,
        reason: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        clean_title = re.sub(r"\s+", " ", str(title or "").strip())
        title_key = self._song_title_key(clean_title)
        if not title_key:
            return await interaction.followup.send("❌ Invalid song title.", ephemeral=True)

        server_norm = self._normalize_song_server(server)
        diff_norm = self._normalize_song_diff(diff)

        async with self.db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id
                FROM banned_songs
                WHERE active = true
                  AND server = $1
                  AND title_key = $2
                  AND COALESCE(diff, '*') = COALESCE($3, '*')
                LIMIT 1
                """,
                server_norm,
                title_key,
                diff_norm,
            )
            if existing:
                await conn.execute(
                    """
                    UPDATE banned_songs
                    SET title = $1,
                        reason = $2,
                        created_by = $3,
                        created_at = now()
                    WHERE id = $4
                    """,
                    clean_title,
                    reason,
                    interaction.user.id,
                    existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO banned_songs
                        (song_id, title, title_key, diff, server, reason, created_by, active)
                    VALUES
                        (NULL, $1, $2, $3, $4, $5, $6, true)
                    """,
                    clean_title,
                    title_key,
                    diff_norm,
                    server_norm,
                    reason,
                    interaction.user.id,
                )

        await interaction.followup.send(
            f"✅ Banned song title: **{clean_title}** | server: `{server_norm}` | diff: `{diff_norm or 'all'}`",
            ephemeral=True,
        )

    @app_commands.command(name="songbanid", description="Ban song by ID from queue.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        song_id="Music ID from the catalog.",
        server="all, en or jp.",
        diff="Optional difficulty.",
        reason="Optional reason.",
    )
    async def songbanid(
        self,
        interaction: discord.Interaction,
        song_id: int,
        server: str = "all",
        diff: str | None = None,
        reason: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        server_norm = self._normalize_song_server(server)
        diff_norm = self._normalize_song_diff(diff)

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT title
                FROM songs
                WHERE id = $1
                  AND ($2 = 'all' OR server = $2)
                ORDER BY server
                LIMIT 1
                """,
                int(song_id),
                server_norm,
            )
            title = str(row["title"]) if row else f"ID {song_id}"
            title_key = self._song_title_key(title)

            existing = await conn.fetchrow(
                """
                SELECT id
                FROM banned_songs
                WHERE active = true
                  AND (song_id = $1 OR title_key = $4)
                  AND server = $2
                  AND COALESCE(diff, '*') = COALESCE($3, '*')
                LIMIT 1
                """,
                int(song_id),
                server_norm,
                diff_norm,
                title_key,
            )
            if existing:
                await conn.execute(
                    """
                    UPDATE banned_songs
                    SET title = $1,
                        title_key = $2,
                        reason = $3,
                        created_by = $4,
                        created_at = now()
                    WHERE id = $5
                    """,
                    title,
                    title_key,
                    reason,
                    interaction.user.id,
                    existing["id"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO banned_songs
                        (song_id, title, title_key, diff, server, reason, created_by, active)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, true)
                    """,
                    int(song_id),
                    title,
                    title_key,
                    diff_norm,
                    server_norm,
                    reason,
                    interaction.user.id,
                )

        await interaction.followup.send(
            f"✅ Banned song ID: `{song_id}` | title: **{title}** | server: `{server_norm}` | diff: `{diff_norm or 'all'}`",
            ephemeral=True,
        )

    @app_commands.command(name="songunban", description="Unban song by title.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    async def songunban(
        self,
        interaction: discord.Interaction,
        title: str,
        server: str = "all",
        diff: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        title_key = self._song_title_key(title)
        server_norm = self._normalize_song_server(server)
        diff_norm = self._normalize_song_diff(diff)

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE banned_songs
                SET active = false
                WHERE active = true
                  AND title_key = $1
                  AND ($2 = 'all' OR server = $2 OR server = 'all')
                  AND ($3::text IS NULL OR diff = $3 OR diff IS NULL)
                RETURNING id
                """,
                title_key,
                server_norm,
                diff_norm,
            )

        await interaction.followup.send(
            f"✅ Unbanned {len(rows)} title ban(s)." if rows else "ℹ️ No active title ban found.",
            ephemeral=True,
        )

    @app_commands.command(name="songunbanid", description="Unban song by ID.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    async def songunbanid(
        self,
        interaction: discord.Interaction,
        song_id: int,
        server: str = "all",
        diff: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        server_norm = self._normalize_song_server(server)
        diff_norm = self._normalize_song_diff(diff)

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE banned_songs
                SET active = false
                WHERE active = true
                  AND song_id = $1
                  AND ($2 = 'all' OR server = $2 OR server = 'all')
                  AND ($3::text IS NULL OR diff = $3 OR diff IS NULL)
                RETURNING id
                """,
                int(song_id),
                server_norm,
                diff_norm,
            )

        await interaction.followup.send(
            f"✅ Unbanned {len(rows)} ID ban(s)." if rows else "ℹ️ No active ID ban found.",
            ephemeral=True,
        )

    @app_commands.command(name="songbans", description="List active song bans.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    async def songbans(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT song_id, title, server, diff, reason, created_at
                FROM banned_songs
                WHERE active = true
                ORDER BY created_at DESC
                LIMIT 25
                """
            )

        if not rows:
            return await interaction.followup.send("No active song bans.", ephemeral=True)

        lines = []
        for r in rows:
            id_part = f"ID {r['song_id']} · " if r["song_id"] is not None else ""
            diff_part = r["diff"] or "all diffs"
            reason_part = f" — {r['reason']}" if r["reason"] else ""
            lines.append(
                f"• {id_part}**{r['title']}** | `{r['server']}` | `{diff_part}`{reason_part}"
            )

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="forfeit", description="Vote to forfeit the current room."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def forfeit(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not isinstance(ch, discord.Thread):
            return await interaction.response.send_message(
                "This command must be used inside a room thread.", ephemeral=True
            )

        room_id, room = self._find_room_by_thread_id(ch.id)
        if room is None or room_id is None:
            return await interaction.response.send_message(
                "This does not look like an active room thread.", ephemeral=True
            )

        if room.get("finished") or room.get("closed"):
            return await interaction.response.send_message(
                "This room is already closed.", ephemeral=True
            )

        valid_player_ids = self._room_player_ids(room)
        if interaction.user.bot or interaction.user.id not in valid_player_ids:
            return await interaction.response.send_message(
                "Only valid players from this room can start a forfeit vote.",
                ephemeral=True,
            )

        lock = self._forfeit_locks.setdefault(ch.id, asyncio.Lock())
        if lock.locked():
            return await interaction.response.send_message(
                "A forfeit vote is already in progress for this room.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        async with lock:
            if room.get("finished") or room.get("closed"):
                return await interaction.followup.send(
                    "This room is already closed.", ephemeral=True
                )

            if room.get("forfeit_in_progress"):
                return await interaction.followup.send(
                    "A forfeit vote is already in progress for this room.",
                    ephemeral=True,
                )

            room["forfeit_in_progress"] = True
            room["forfeit_votes"] = {}

            vote_msg = None
            try:
                player_mentions = " ".join(f"<@{uid}>" for uid in sorted(valid_player_ids))
                vote_msg = await self._safe_channel_send(
                    ch,
                    "🏳️ **Forfeit vote started.**\n"
                    "React with ✅ to accept closing the room.\n"
                    "React with ❌ to reject closing the room.\n"
                    f"Rule: **3 or more ✅ closes the room**. Tie or more ❌ rejects it.\n"
                    f"Only room players count. Timeout: **{int(FORFEIT_TIMEOUT_SEC)}s**.\n"
                    f"Players: {player_mentions}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )

                if vote_msg is None:
                    return await interaction.followup.send(
                        "Could not start the forfeit vote because Discord did not accept the message.",
                        ephemeral=True,
                    )

                for emoji in ("✅", "❌"):
                    try:
                        await vote_msg.add_reaction(emoji)
                    except Exception:
                        pass

                await interaction.followup.send(
                    "Forfeit vote started in this room.", ephemeral=True
                )

                votes: dict[int, str] = {}
                deadline = asyncio.get_running_loop().time() + float(FORFEIT_TIMEOUT_SEC)

                def check(reaction: discord.Reaction, user: discord.User):
                    if user.bot:
                        return False
                    if reaction.message.id != vote_msg.id:
                        return False
                    if str(reaction.emoji) not in ("✅", "❌"):
                        return False
                    return int(user.id) in valid_player_ids

                while True:
                    yes_count = sum(1 for v in votes.values() if v == "✅")
                    if yes_count >= 3:
                        break

                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break

                    try:
                        reaction, user = await self.bot.wait_for(
                            "reaction_add",
                            timeout=remaining,
                            check=check,
                        )
                    except asyncio.TimeoutError:
                        break
                    except Exception:
                        log.exception("forfeit wait_for failed for thread=%s", ch.id)
                        break

                    uid = int(user.id)
                    if uid in votes:
                        # Un usuario solo cuenta una vez. La primera reacción válida gana.
                        continue

                    votes[uid] = str(reaction.emoji)
                    room["forfeit_votes"] = {str(k): v for k, v in votes.items()}

                yes_count = sum(1 for v in votes.values() if v == "✅")
                no_count = sum(1 for v in votes.values() if v == "❌")

                approved = yes_count >= 3
                if approved:
                    room["finished"] = True
                    room["closed"] = True
                    room["forfeit_in_progress"] = False

                    try:
                        await self._remove_queue_roles(self._room_members(interaction.guild, room))
                    except Exception:
                        log.exception("Failed to remove queue roles after forfeit")

                    await self._safe_channel_send(
                        ch,
                        f"✅ **Forfeit approved.** Closing room. (✅ {yes_count} / ❌ {no_count})",
                    )

                    try:
                        self.bot.dispatch("room_finished", room_id)
                    except Exception:
                        pass

                    try:
                        asyncio.create_task(self.close_room(ch, delay=5, delete=True))
                    except Exception:
                        log.exception("Failed to schedule close_room after forfeit")

                    return

                if yes_count == no_count:
                    result_text = (
                        f"⚖️ **Forfeit rejected by tie.** Room stays open. "
                        f"(✅ {yes_count} / ❌ {no_count})"
                    )
                elif no_count > yes_count:
                    result_text = (
                        f"❌ **Forfeit rejected.** Room stays open. "
                        f"(✅ {yes_count} / ❌ {no_count})"
                    )
                else:
                    result_text = (
                        f"❌ **Forfeit failed.** Not enough ✅ votes to close the room. "
                        f"Need 3 ✅. (✅ {yes_count} / ❌ {no_count})"
                    )

                await self._safe_channel_send(ch, result_text)

            finally:
                room["forfeit_in_progress"] = False
                room.pop("forfeit_votes", None)
                if vote_msg is not None:
                    room["last_forfeit_vote_message_id"] = vote_msg.id

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
                ganador = "✅" if counts["✅"] > counts["❎"] else "❎"

            try:
                await vote_msg.edit(
                    content=f"🗳️ Voting closed → {ganador}  (✅ {counts['✅']} / ❎ {counts['❎']})"
                )
            except Exception:
                pass

            if ganador != "✅":
                await ctx.send("Submit declined. There might be an error, try doing it again.")
                return

            # ==============================
            # VOTO APROBADO → aplicar MMR
            # ==============================
            result_id = await self._next_result_id()

            # --- resolver seasons ---
            season_row = await self.get_current_season()
            if not season_row:
                await ctx.send("❌ No active season found.")
                return

            season_id = int(season_row["id"])
            season_name = str(season_row["name"])

            prev_row = await self._get_previous_season(season_id)
            prev_season_id = int(prev_row["id"]) if prev_row else None

            # --- map prev mmr SOLO para season placement (batch) ---
            sp_ids = [m.id for m in players if self._has_season_placement_role(m)]
            prev_mmr_map: dict[int, int] = {}
            if prev_season_id and sp_ids:
                prev_mmr_map = await self._fetch_mmrs_batch_for_season(prev_season_id, sp_ids)

            # --- map countries para mostrar flags en results.png ---
            # Esto NO escribe ni modifica players.country en PostgreSQL.
            country_map = await self._fetch_country_codes_batch([m.id for m in players])

            # --- matches previos por usuario en esta season ---
            # Se consulta ANTES de insertar el match actual.
            # Solo Placement normal + 0 partidas previas muestra [PLACEMENT] en results.png.
            matches_played_before_map = await self._fetch_matches_played_batch(
                season_id,
                [m.id for m in players],
            )

            # --- construir players_list ---
            players_list = []
            for member in players:
                uid = member.id
                cc, stats = by_id[uid]
                old, current_role = await self.fetch_player(uid)

                # Bandera visual para results.png:
                # 1) Primero usa players.country guardado en PostgreSQL.
                # 2) Si no hay bandera guardada, usa [CC] del submit si fue enviado.
                # 3) Si tampoco hay bandera válida, usa assets/flags/unknown.jpg.
                # IMPORTANTE: esto NO actualiza players.country ni cambia la bandera guardada.
                submit_cc = (cc or "").upper().strip()
                stored_cc = (country_map.get(uid) or "").upper().strip()
                display_cc = country_code_for_results(stored_cc, submit_cc)
                total_points = sum(s * w for s, w in zip(stats, [4, 3, 2, 1, 0]))

                is_season_placement = self._has_season_placement_role(member)
                is_rank_placement = (current_role == "Placement")

                matches_played_before = int(matches_played_before_map.get(uid, 0))

                # Visual solamente para results.png:
                # Placement normal y Season Placement deben usar el mismo tratamiento
                # visual de placement en la primera partida de la season.
                show_placement_badge = bool(
                    (is_rank_placement or is_season_placement)
                    and matches_played_before == 0
                )

                prev_season_mmr = int(prev_mmr_map.get(uid, 0)) if is_season_placement else 0

                players_list.append(
                    {
                        "member": member,
                        "cc": display_cc,
                        "stats": stats,
                        "total": total_points,
                        "old": int(old),
                        "mmr_actual": int(old),  # base; se sobreescribe si es placement
                        "role": current_role,
                        "country_code": display_cc,
                        "was_placement": bool(is_season_placement or is_rank_placement),
                        "is_season_placement": bool(is_season_placement),
                        "prev_season_mmr": prev_season_mmr,
                        "is_rank_placement": bool(is_rank_placement),
                        "matches_played_before": matches_played_before,
                        "show_placement_badge": show_placement_badge,
                        "seed_mmr": None,
                        "seed_rank": None,
                    }
                )

            # --- aplicar seeds (FUERA del for member) ---
            for p in players_list:
                if p.get("is_season_placement", False):
                    prev_mmr = int(p.get("prev_season_mmr") or 0)
                    seeded = self._clamp_mmr(prev_mmr - SEASON_RESET_PENALTY)
                    p["mmr_actual"] = seeded
                    p["seed_mmr"] = seeded
                    p["seed_rank"] = get_rank_from_mmr(seeded)
                    p["was_placement"] = True
                    continue

                if p.get("is_rank_placement", False):
                    seed_mmr, seed_rank = self.placement_seed_from_stats(p["stats"])
                    p["mmr_actual"] = int(seed_mmr)
                    p["seed_mmr"] = int(seed_mmr)
                    p["seed_rank"] = seed_rank
                    p["was_placement"] = True

            # Room MMR (con seeds)
            room_mmr = (
                round(sum(int(p["mmr_actual"]) for p in players_list) / len(players_list))
                if players_list else None
            )

            # Orden final: puntos desc; empate → Placement primero
            def sort_key(p):
                return (p["total"], 1 if p["was_placement"] else 0)

            players_list.sort(key=sort_key, reverse=True)

            ratings = [int(p["mmr_actual"]) for p in players_list]
            order = list(range(len(players_list)))
            deltas = self.pairwise_deltas_simple(ratings, order, K=50, scale=400, cap=39)
            deltas = self._apply_tie_logic_placement_priority(players_list, deltas)

            summary = []

            for idx, p in enumerate(players_list, start=1):
                mmr_prev = int(p["mmr_actual"])
                raw_delta = int(deltas[idx - 1])

                mmr_delta = raw_delta
                mmr_final = self._clamp_mmr(mmr_prev + mmr_delta)
                role_name = get_rank_from_mmr(mmr_final)

                p["mmr_prev"] = mmr_prev
                p["mmr_delta"] = mmr_delta
                p["mmr_final"] = mmr_final

                s_p, s_g, s_g2, s_b, s_m = p["stats"]

                await self.db_pool.execute(
                    """
                    INSERT INTO seasonplayers (season_id, user_id, mmr, role, is_season_placement)
                    VALUES ($1, $2, $3, $4, FALSE)
                    ON CONFLICT (season_id, user_id)
                    DO UPDATE SET
                        mmr = EXCLUDED.mmr,
                        role = EXCLUDED.role,
                        is_season_placement = FALSE
                    """,
                    season_id,
                    p["member"].id,
                    mmr_final,
                    role_name,
                )

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
                    s_p, s_g, s_g2, s_b, s_m,
                    mmr_prev, mmr_final, mmr_delta,
                    bool(p["was_placement"]),
                    mmr_delta >= 0,
                )

                await self.db_pool.execute(
                    """
                    UPDATE players
                    SET role = $2
                    WHERE user_id = $1
                    """,
                    p["member"].id,
                    role_name,
                )

                await self._apply_rank_role(
                    ctx.guild,
                    p["member"],
                    role_name,
                    reason=f"Submit result #{result_id}",
                )

                summary.append(
                    (
                        idx,
                        p["member"].display_name,
                        p["total"],
                        p["stats"],
                        mmr_prev,
                        mmr_delta,
                        mmr_final,
                        p["was_placement"],
                        role_name,  # color del board basado en el rango FINAL
                        p["country_code"],  # ISO2 o __UNKNOWN_FLAG__ para assets/flags/unknown.jpg
                        p.get("show_placement_badge", False),  # SOLO visual en results.png
                        p.get("is_season_placement", False),  # distingue [SEASON PLACEMENT] en results.png
                    )
                )

            # Actualizar títulos temporales CURRENT de la season activa después de recalcular MMR.
            try:
                async with self.db_pool.acquire() as conn:
                    await self._sync_current_season_top_titles(conn, season_id, season_name)
            except Exception:
                log.exception("No se pudo sincronizar CURRENT season top titles después del submit")

            # =========================
            # PUBLICAR RESULTADOS 1 SOLA VEZ
            # =========================
            join_parent = ctx.channel.parent
            result_chan_id = JOIN_TO_RESULTS.get(join_parent.id) if join_parent else None
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
                songs_block = "\n**Songs (Lv / Diff / Notes):**\n" + "\n".join(song_lines)

            header_line = f"ID: {result_id}{gap_text}{songs_block}"

            submitted_at_dt = self._now_lima()
            submitted_at_text = submitted_at_dt.strftime("%d/%m/%Y %H:%M") + " UTC-5"

            with Image.open("assets/logo.png") as logo_img:
                image_kwargs = {
                    "font_path": "assets/rodinntlg_eb.otf",
                    "side_image": logo_img.copy(),
                }

                passed_submitted_at_text = False
                try:
                    sig = inspect.signature(generate_results_image)
                    if "submitted_at_text" in sig.parameters:
                        image_kwargs["submitted_at_text"] = submitted_at_text
                        passed_submitted_at_text = True

                    if "unknown_flag_path" in sig.parameters:
                        image_kwargs["unknown_flag_path"] = UNKNOWN_FLAG_IMAGE_PATH

                    if "unknown_flag_code" in sig.parameters:
                        image_kwargs["unknown_flag_code"] = UNKNOWN_FLAG_CODE
                except (TypeError, ValueError):
                    pass

                buf = generate_results_image(summary, **image_kwargs)

                if not passed_submitted_at_text:
                    buf = self._overlay_submitted_at_text_on_results_image(
                        buf,
                        submitted_at_text,
                        font_path="assets/rodinntlg_eb.otf",
                    )

            buf.seek(0)
            image_bytes = buf.read()

            embed = discord.Embed(
                title=f"RESULTS — {season_name}",
                description=header_line,
                color=0x00F7FF,
            ).set_image(url="attachment://results.png")

            msg_result = await result_chan.send(
                embed=embed,
                file=discord.File(io.BytesIO(image_bytes), "results.png"),
            )

            await ctx.send(
                embed=embed,
                file=discord.File(io.BytesIO(image_bytes), "results.png"),
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

            room["finished"] = True
            room["closed"] = True

            try:
                await self._remove_queue_roles(players)
            except Exception:
                pass

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

            asyncio.create_task(self.close_room(ctx.channel, delay=30, delete=True))
            # pero asegurándote que siga con ESTA MISMA indentación (dentro del try)

        finally:
            room["vote_in_progress"] = False

    async def _apply_exact_rank_role_id(
        self,
        guild: discord.Guild,
        member_or_id,
        target_role_id: int,
        reason: str = "",
    ) -> bool:
        """
        Aplica exactamente un rol competitivo y limpia los demás roles competitivos.
        Esto permite restaurar Season Placement al reingresar al servidor, porque
        Discord elimina roles cuando alguien sale del guild.
        """
        rank_role_ids = set(RANK_ROLE_IDS.values()) | {SEASON_PLACEMENT_ROLE_ID}

        member = (
            member_or_id
            if isinstance(member_or_id, discord.Member)
            else guild.get_member(int(member_or_id))
        )
        if member is None:
            try:
                member = await guild.fetch_member(int(member_or_id))
            except Exception:
                return False

        target_role = guild.get_role(int(target_role_id))
        if not target_role:
            return False

        keep = [r for r in member.roles if r.id not in rank_role_ids]
        new_roles = list(dict.fromkeys(keep + [target_role]))

        try:
            await member.edit(
                roles=new_roles,
                reason=reason or "Sync current-season rank role",
            )
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _sync_member_rank_role_from_current_season(
        self,
        guild: discord.Guild,
        member: discord.Member,
        reason: str = "",
        *,
        preserve_existing_season_placement: bool = False,
    ) -> bool:
        """
        Fuente de verdad: seasonplayers de la season activa.

        - is_season_placement=true y 0 matches en la season actual -> rol Season Placement.
        - role='Placement' -> rol Placement.
        - cualquier otro role -> rol de rango correspondiente.

        No cambia MMR ni escribe la DB; solo repara roles en Discord.
        """
        if member.bot or self.db_pool is None:
            return False

        # /c no debe convertir Season Placement -> rango antes de jugar.
        # El cambio real de rol se hace después del submit aprobado.
        if preserve_existing_season_placement and self._has_season_placement_role(member):
            return True

        season_row = await self.get_current_season()
        if not season_row:
            return False

        season_id = int(season_row["id"])

        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COALESCE(sp.role, 'Placement') AS role,
                    COALESCE(sp.is_season_placement, false) AS is_season_placement,
                    COALESCE(mh.matches_played, 0)::int AS matches_played
                FROM seasonplayers sp
                LEFT JOIN (
                    SELECT user_id, COUNT(*)::int AS matches_played
                    FROM match_history
                    WHERE season_id = $1 AND user_id = $2
                    GROUP BY user_id
                ) mh ON mh.user_id = sp.user_id
                WHERE sp.season_id = $1
                  AND sp.user_id = $2
                """,
                season_id,
                int(member.id),
            )

        if not row:
            return False

        role_name = str(row["role"] or "Placement").strip()
        is_season_placement = bool(row["is_season_placement"])
        matches_played = int(row["matches_played"] or 0)

        if is_season_placement and matches_played == 0:
            target_role_id = SEASON_PLACEMENT_ROLE_ID
        else:
            target_role_id = RANK_ROLE_IDS.get(role_name, PLACEMENT_ROLE_ID)

        return await self._apply_exact_rank_role_id(
            guild,
            member,
            int(target_role_id),
            reason=reason or "Sync current-season rank role",
        )

    async def _apply_rank_role(
        self, guild: discord.Guild, member_or_id, role_name: str, reason: str = ""
    ) -> bool:
        rank_role_ids = set(RANK_ROLE_IDS.values()) | {SEASON_PLACEMENT_ROLE_ID}

        member = (
            member_or_id
            if isinstance(member_or_id, discord.Member)
            else guild.get_member(int(member_or_id))
        )
        if member is None:
            try:
                member = await guild.fetch_member(int(member_or_id))
            except Exception:
                return False

        role_name = (role_name or "Placement").strip()
        if role_name not in RANK_ROLE_IDS:
            role_name = "Placement"

        target_role = guild.get_role(RANK_ROLE_IDS[role_name])
        if not target_role:
            target_role = guild.get_role(RANK_ROLE_IDS["Placement"])
            if not target_role:
                return False

        keep = [r for r in member.roles if r.id not in rank_role_ids]
        new_roles = list(dict.fromkeys(keep + [target_role]))

        try:
            await member.edit(
                roles=new_roles,
                reason=reason or f"Apply rank {role_name}",
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
                INSERT INTO seasonplayers (season_id, user_id, mmr, role, is_season_placement, starting_mmr)
                VALUES ($1, $2, $3, $4, FALSE, NULL)
                ON CONFLICT (season_id, user_id)
                DO UPDATE SET
                    mmr = EXCLUDED.mmr,
                    role = EXCLUDED.role,
                    is_season_placement = FALSE,
                    starting_mmr = NULL
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
            season_row = await self.get_current_season()
            if not season_row:
                return await ctx.reply("❌ No active season found.")

            ok = await self._sync_member_rank_role_from_current_season(
                ctx.guild,
                member,
                reason="Manual fix_user_rank_roles",
            )

            if not ok:
                return await ctx.reply("❌ User has no row in current season or I could not edit the role.")

            await ctx.reply("Done!")
        except Exception as e:
            await ctx.reply(str(e))


async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))
