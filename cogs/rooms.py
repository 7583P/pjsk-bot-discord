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
    """Cog unificado para matchmaking y visualización de salas."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # rooms: { room_id: { "players": [...], "category_id": int } }
        self.rooms: dict[int, dict] = {}
        # posted_messages: { category_id: discord.Message }
        self.posted_messages: dict[int, discord.Message] = {}

        self.update_rooms.start()
        bot.add_listener(self.on_room_finished, 'room_finished')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_finished)

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.rooms")

    @app_commands.command(name="c", description="Crear sala por MMR")
    async def cmd_c(self, interaction: discord.Interaction):
        # 1) Generar ID de sala
        new_room_id = max(self.rooms.keys(), default=0) + 1

        # 2) Capturar categoría y crear sala
        cat_id = interaction.channel.category_id
        self.rooms[new_room_id] = {
            "players": [interaction.user],
            "category_id": cat_id,
        }

        # 3) Disparar actualización inmediata
        await self._do_update()

        # 4) Confirmación al usuario
        await interaction.response.send_message(
            f"Sala **{new_room_id}** creada en tu categoría.", 
            ephemeral=True
        )

    @app_commands.command(name="d", description="Salir y cerrar sala si queda vacía")
    async def cmd_d(self, interaction: discord.Interaction):
        # 1) Encontrar la sala del usuario
        target = None
        for rid, info in self.rooms.items():
            if interaction.user in info["players"]:
                info["players"].remove(interaction.user)
                target = rid
                break

        if not target:
            return await interaction.response.send_message(
                "No estás en ninguna sala activa.", ephemeral=True
            )

        # 2) Si quedó vacía, eliminarla
        if not self.rooms[target]["players"]:
            del self.rooms[target]

        # 3) Refrescar listado
        await self._do_update()
        await interaction.response.send_message(
            f"Te has salido de la sala **{target}**.", ephemeral=True
        )

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_finished(self, room_id: int):
        # (no usado en este enfoque, pero lo dejamos por compatibilidad)
        await self._do_update()

    async def _do_update(self):
        try:
            now = int(time.time())

            # Agrupar salas por categoría
            grouped: dict[int, list[tuple[int,int,list[tuple[discord.Member,int]]]]] = {}
            for rid, info in self.rooms.items():
                cat = info["category_id"]
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue
                pdata, mmr_vals = [], []
                for mem in info["players"]:
                    if isinstance(mem, discord.Member):
                        try:
                            mmr, _ = await self.fetch_player(mem.id)
                        except:
                            mmr = 0
                        pdata.append((mem, mmr))
                        mmr_vals.append(mmr)
                avg = sum(mmr_vals)//len(mmr_vals) if mmr_vals else 0
                grouped.setdefault(cat, []).append((rid, avg, pdata))

            # Para cada categoría, enviar/editar su canal de rooms
            for cat_id, chan_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = self.bot.get_channel(chan_id)
                if not channel:
                    continue

                lines: list[str] = []
                rooms_list = grouped.get(cat_id, [])
                if not rooms_list:
                    lines.append("**No active rooms**")
                else:
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

        except Exception as e:
            print(f"›› [Rooms] Error: {e}")

    # --- placeholder para tu función real de fetch_player ---
    async def fetch_player(self, user_id: int) -> tuple[int, dict]:
        """
        Debes reemplazar esto por tu lógica real que consulta la DB
        y devuelve (mmr, otros_datos).
        """
        return 1000, {}

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
