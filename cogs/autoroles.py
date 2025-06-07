import os
import aiosqlite
import discord
from discord.ext import commands

# ─────────── CONFIGURACIÓN ───────────
DB_PATH   = "matchmaking.db"
GUILD_ID  = int(os.getenv("GUILD_ID", "0"))  # Asegúrate de que la variable GUILD_ID esté definida en tu entorno

class AutoRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ─────────── SETUP / TEARDOWN ───────────
    async def cog_load(self):
        """
        Se ejecuta cuando se carga el cog.
        Conecta a la base de datos y se asegura de que exista la tabla players.
        """
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                mmr     INTEGER DEFAULT 0,
                role    TEXT    DEFAULT 'Placement'
            );
        """)
        await self.db.commit()
        print("[AutoRoles] Tabla 'players' creada o ya existía.")

    async def cog_unload(self):
        """
        Se ejecuta cuando se descarga el cog.
        Cierra la conexión a la base de datos.
        """
        await self.db.close()

    # ─────────── MÉTODO AUXILIAR: fetch_player ───────────
    async def fetch_player(self, user_id: int):
        """
        Recupera (mmr, role) de la tabla players para user_id.
        Si no existe, lo crea con mmr=0 y role='Placement' y devuelve (0, 'Placement').
        """
        async with self.db.execute(
            "SELECT mmr, role FROM players WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            return row  # Devuelve (mmr, role)

        # Si no existe registro, lo insertamos como Placement
        await self.db.execute(
            "INSERT INTO players (user_id, mmr, role) VALUES (?, 0, 'Placement')",
            (user_id,)
        )
        await self.db.commit()
        return (0, "Placement")

    # ─────────── LISTENER on_member_join ───────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """
        Cada vez que un usuario nuevo se une al servidor:
        1) Llama a fetch_player para asegurarse de que haya una fila en la BD.
        2) Si mmr == 0, fuerza el role_name a "Placement".
        3) Busca el rol en Discord por nombre y, si existe, se lo asigna al miembro.
        """
        # Solo actuamos si el miembro pertenece al GUILD_ID configurado
        if member.guild.id != GUILD_ID:
            return

        print(f"[AutoRoles] on_member_join disparado para {member} ({member.id})")

        try:
            mmr, role_name = await self.fetch_player(member.id)

            # Forzamos Placement si mmr == 0 (aunque fetch_player ya lo asigna)
            if mmr == 0:
                role_name = "Placement"

            # Obtener el objeto Role en el servidor
            guild_role = discord.utils.get(member.guild.roles, name=role_name)
            if guild_role is None:
                print(f"[AutoRoles] ❌ No existe el rol '{role_name}' en {member.guild.name}")
                return

            # Asignar el rol al miembro
            await member.add_roles(guild_role, reason="Asignación automática al unirse")
            print(f"[AutoRoles] ✅ Asigné rol '{role_name}' a {member.name}")
        except Exception as e:
            print(f"[AutoRoles] ❌ Excepción en on_member_join: {e}")

async def setup(bot: commands.Bot):
    """
    Punto de entrada para cargar el cog en el bot.
    """
    await bot.add_cog(AutoRoles(bot))
