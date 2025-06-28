import discord
import asyncio
import time
from discord.ext import commands, tasks

# 1) Mapea category_id → canal "rooms"
CATEGORY_TO_ROOM_CHANNEL = {
    1371306302671687710: 1371307831176728706,  # Categoría A → canal Rooms A
    1371951461612912802: 1388515368934309978,  # Categoría B → canal Rooms B
}

class Matchmaking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Diccionario de rooms: room_id → { players: [...], category_id: int }
        self.rooms: dict[int, dict] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.matchmaking")

    @commands.tree.command(name="c", description="Emparejar por MMR")
    async def cmd_c(self, interaction: discord.Interaction):
        cat_id = interaction.channel.category_id
        if cat_id not in CATEGORY_TO_ROOM_CHANNEL:
            await interaction.response.send_message(
                "❌ No puedes usar `/c` en esta categoría.", ephemeral=True
            )
            return

        # Lógica de emparejamiento (ejemplo básico)
        room_id = len(self.rooms) + 1
        self.rooms[room_id] = {
            "players": [interaction.user],
            "category_id": cat_id,
        }

        # Notifica a Rooms cog que una sala ha sido actualizada
        self.bot.dispatch('room_updated', room_id)

        await interaction.response.send_message(
            f"🔔 Sala {room_id} creada en la categoría <#{cat_id}>.", ephemeral=True
        )

class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Guarda el mensaje de estado para cada canal Rooms
        self.posted_messages: dict[int, discord.Message] = {}
        self.update_rooms.start()
        bot.add_listener(self.on_room_updated, 'room_updated')
        bot.add_listener(self.on_room_finished, 'room_finished')

    def cog_unload(self):
        self.update_rooms.cancel()
        self.bot.remove_listener(self.on_room_updated)
        self.bot.remove_listener(self.on_room_finished)

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int | None = None):
        await self._do_update()

    async def _do_update(self):
        mm_cog = self.bot.get_cog("Matchmaking")
        all_rooms = getattr(mm_cog, 'rooms', {}) or {}

        # Itera por cada categoría configurada
        for cat_id, room_channel_id in CATEGORY_TO_ROOM_CHANNEL.items():
            channel = self.bot.get_channel(room_channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            # Sólo salas creadas en esta categoría
            rooms_in_cat = {
                rid: info
                for rid, info in all_rooms.items()
                if info.get("category_id") == cat_id
            }

            lines: list[str] = []
            if not rooms_in_cat:
                lines.append("**No hay salas activas en esta categoría**")
            else:
                rooms_list = []
                for rid, info in rooms_in_cat.items():
                    players = info.get('players', [])
                    pdata, mmr_vals = [], []
                    for mem in players:
                        if isinstance(mem, discord.Member):
                            try:
                                mmr_val, _ = await mm_cog.fetch_player(mem.id)
                            except:
                                mmr_val = 0
                            pdata.append((mem, mmr_val))
                            mmr_vals.append(mmr_val)
                    avg = sum(mmr_vals)//len(mmr_vals) if mmr_vals else 0
                    rooms_list.append((rid, avg, pdata))

                rooms_list.sort(key=lambda x: x[1], reverse=True)
                for rid, avg, pdata in rooms_list:
                    lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                    for mem, mmr in pdata:
                        lines.append(f"{mem.display_name} ({mmr})")
                    lines.append("")

            lines.append(f"**Last Updated:** <t:{int(time.time())}:R>")
            content = "\n".join(lines)

            # Envía o edita el mensaje en este canal
            msg = self.posted_messages.get(channel.id)
            if not msg:
                sent = await channel.send(content)
                self.posted_messages[channel.id] = sent
            else:
                await msg.edit(content=content)

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        mm_cog = self.bot.get_cog("Matchmaking")
        if mm_cog and room_id in getattr(mm_cog, 'rooms', {}):
            del mm_cog.rooms[room_id]
            await self._do_update()

# Setup único para registrar ambos cogs
en
async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))
    await bot.add_cog(Rooms(bot))
    print("✅ Cogs cargados: Matchmaking, Rooms")
