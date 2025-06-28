# cogs/rooms.py

import discord
import asyncio
import time
from discord import app_commands
from discord.ext import commands, tasks

# Mapea category_id → canal "rooms" correspondiente
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk queue → rooms pjsk queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}

class Rooms(commands.Cog):
    """Cog unificado: matchmaking y publicación de salas por categoría."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # rooms: { room_id: { "players": [Member, ...], "category_id": int } }
        self.rooms: dict[int, dict] = {}

        # posted_messages: { category_id: discord.Message }
        self.posted_messages: dict[int, discord.Message] = {}

        # Arranca el loop de actualización
        self.update_rooms.start()

    def cog_unload(self):
        self.update_rooms.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.rooms")

    @app_commands.command(name="c", description="Crear sala por MMR")
    async def cmd_c(self, interaction: discord.Interaction):
        # 1) Generar un nuevo ID de sala
        new_room_id = max(self.rooms.keys(), default=0) + 1

        # 2) Registrar la sala con su categoría y primer jugador
        cat_id = interaction.channel.category_id
        self.rooms[new_room_id] = {
            "players": [interaction.user],
            "category_id": cat_id,
        }

        # 3) Actualizar inmediatamente los canales de rooms
        await self._do_update()

        # 4) Responder en privado
        await interaction.response.send_message(
            f"Sala **{new_room_id}** creada en tu categoría.", 
            ephemeral=True
        )

    @app_commands.command(name="d", description="Salir y cerrar sala si queda vacía")
    async def cmd_d(self, interaction: discord.Interaction):
        # 1) Buscar la sala donde está el usuario
        target = None
        for rid, info in self.rooms.items():
            if interaction.user in info["players"]:
                info["players"].remove(interaction.user)
                target = rid
                break

        # 2) Si no estaba en ninguna sala
        if target is None:
            return await interaction.response.send_message(
                "No estás en ninguna sala activa.", 
                ephemeral=True
            )

        # 3) Si la sala quedó vacía, la elimino
        if not self.rooms[target]["players"]:
            del self.rooms[target]

        # 4) Actualizar canales y avisar al usuario
        await self._do_update()
        await interaction.response.send_message(
            f"Te has salido de la sala **{target}**.", 
            ephemeral=True
        )

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def _do_update(self):
        now = int(time.time())

        # 1) Agrupar salas por categoría
        grouped: dict[int, list[tuple[int,int,list[tuple[discord.Member,int]]]]] = {}
        for rid, info in self.rooms.items():
            cat = info["category_id"]
            if cat not in CATEGORY_TO_ROOM_CHANNEL:
                continue

            # Recojo players + sus MMRs
            pdata, mmr_vals = [], []
            for mem in info["players"]:
                mmr = 0
                if isinstance(mem, discord.Member):
                    try:
                        mmr, _ = await self.fetch_player(mem.id)
                    except:
                        mmr = 0
                pdata.append((mem, mmr))
                mmr_vals.append(mmr)

            avg = sum(mmr_vals)//len(mmr_vals) if mmr_vals else 0
            grouped.setdefault(cat, []).append((rid, avg, pdata))

        # 2) Para cada categoría, construir y enviar/editar el mensaje
        for cat_id, chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
            channel = self.bot.get_channel(chan_id)
            if not channel:
                continue

            lines: list[str] = []
            rooms_list = grouped.get(cat_id, [])
            if not rooms_list:
                lines.append("**No active rooms**")
            else:
                # Ordenar por MMR promedio descendente
                rooms_list.sort(key=lambda x: x[1], reverse=True)
                for rid, avg, pdata in rooms_list:
                    lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                    for mem, mmr in pdata:
                        lines.append(f"{mem.display_name} ({mmr})")
                    lines.append("")

            lines.append(f"**Last Updated:** <t:{now}:R>")
            content = "\n".join(lines)

            prev = self.posted_messages.get(cat_id)
            if not prev or prev.channel.id != chan_id:
                msg = await channel.send(content)
                self.posted_messages[cat_id] = msg
            else:
                await prev.edit(content=content)

    async def fetch_player(self, user_id: int) -> tuple[int, dict]:
        """
        Aquí va tu lógica real de consulta a BD para sacar el MMR.
        Mientras tanto, devolvemos 1000 para pruebas.
        """
        return 1000, {}

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
