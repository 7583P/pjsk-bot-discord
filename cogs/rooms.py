# cogs/rooms.py

import discord
import asyncio
import time
from discord.ext import commands, tasks

# Ajusta estos IDs a tu servidor
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk queue → rooms pjsk queue
    1371951461612912802: 1388515368934309978,  # jp-pjsk    → rooms jp-pjsk
}

class Rooms(commands.Cog):
    """Cog que lee las salas de Matchmaking.rooms y publica/actualiza
       el listado en cada canal de #rooms según la categoría."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Mensaje enviado en cada canal de rooms, para editarlo luego
        self.posted_messages: dict[int, discord.Message] = {}
        # Arranca el loop de actualización periódica
        self.update_rooms.start()
        # Escucha los eventos disparados en matchmaking.py
        bot.add_listener(self.on_room_updated, 'room_updated')
        bot.add_listener(self.on_room_finished, 'room_finished')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated)
        self.bot.remove_listener(self.on_room_finished)

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.rooms")

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        # Backup refresco periódico
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        """Llamado por matchmaking.py tras /c."""
        await self._do_update()

    async def on_room_finished(self, room_id: int):
        """Llamado por matchmaking.py al cerrar una sala vacía."""
        await asyncio.sleep(1)
        await self._do_update()

    async def _do_update(self):
        try:
            # Obtener el cog de Matchmaking y su diccionario de salas
            mm = self.bot.get_cog("Matchmaking")
            all_rooms = getattr(mm, "rooms", {})

            # Agrupar salas por categoría
            grouped: dict[int, list[tuple[int,int,list[tuple[discord.Member,int]]]]] = {}
            for room_id, info in all_rooms.items():
                cat = info.get("category_id")
                if cat not in CATEGORY_TO_ROOM_CHANNEL:
                    continue

                players = info.get("players", [])
                pdata: list[tuple[discord.Member,int]] = []
                mmr_vals: list[int] = []

                for member in players:
                    if not isinstance(member, discord.Member):
                        continue
                    # Aquí llamamos al fetch_player de matchmaking.py
                    try:
                        mmr, _ = await mm.fetch_player(member.id)
                    except Exception:
                        mmr = 0
                    pdata.append((member, mmr))
                    mmr_vals.append(mmr)

                avg = sum(mmr_vals)//len(mmr_vals) if mmr_vals else 0
                grouped.setdefault(cat, []).append((room_id, avg, pdata))

            now = int(time.time())

            # Para cada categoría, construye y publica/edita el mensaje en #rooms
            for cat_id, channel_id in CATEGORY_TO_ROOM_CHANNEL.items():
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue

                lines: list[str] = []
                rooms_list = grouped.get(cat_id, [])

                if not rooms_list:
                    lines.append("**No active rooms**")
                else:
                    # Orden descendiente por MMR promedio
                    rooms_list.sort(key=lambda x: x[1], reverse=True)
                    for rid, avg, pdata in rooms_list:
                        lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                        for mem, mmr in pdata:
                            lines.append(f"{mem.display_name} ({mmr})")
                        lines.append("")  # separador

                lines.append(f"**Last Updated:** <t:{now}:R>")
                content = "\n".join(lines)

                prev_msg = self.posted_messages.get(cat_id)
                if not prev_msg or prev_msg.channel.id != channel_id:
                    # Enviar mensaje nuevo
                    msg = await channel.send(content)
                    self.posted_messages[cat_id] = msg
                else:
                    # Editar mensaje existente
                    await prev_msg.edit(content=content)

        except Exception as e:
            print(f"›› [Rooms] Error en _do_update: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Rooms(bot))
