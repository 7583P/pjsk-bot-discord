import discord
import asyncio
import time
from discord.ext import commands, tasks
from discord import app_commands
from typing import Dict, List, Tuple, Optional

# Mapea category_id → canal "rooms" correspondiente
CATEGORY_TO_ROOM_CHANNEL: Dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # Categoría A → canal Rooms A
    1371951461612912802: 1388515368934309978,  # Categoría B → canal Rooms B
}

class Matchmaking(commands.Cog):
    """Cog para gestionar comandos de emparejamiento y creación de salas."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # rooms: { room_id: { players: [...], category_id: int }}
        self.rooms: Dict[int, Dict] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Cog cargado: cogs.matchmaking")

    async def fetch_player(self, member_id: int) -> Tuple[int, Optional[str]]:
        # TODO: Reemplaza con tu lógica de obtención de MMR desde base de datos
        return 0, None

    @app_commands.command(name="c", description="Crear sala por MMR")
    @app_commands.guilds(discord.Object(id=YOUR_GUILD_ID))  # Reemplaza YOUR_GUILD_ID
    async def cmd_c(self, interaction: discord.Interaction):
        cat_id = interaction.channel.category_id
        if cat_id not in CATEGORY_TO_ROOM_CHANNEL:
            return await interaction.response.send_message(
                "❌ No puedes usar `/c` en esta categoría.", ephemeral=True
            )

        room_id = len(self.rooms) + 1
        self.rooms[room_id] = {
            "players": [interaction.user],
            "category_id": cat_id,
        }
        self.bot.dispatch('room_updated', room_id)
        await interaction.response.send_message(
            f"🔔 Sala {room_id} creada en la categoría <#{cat_id}>.", ephemeral=True
        )

class Rooms(commands.Cog):
    """Cog para publicar y actualizar el estado de las salas en sus canales correspondientes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.posted_messages: Dict[int, discord.Message] = {}
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

    async def on_room_updated(self, room_id: int):
        await self._do_update()

    async def _do_update(self):
        mm_cog = self.bot.get_cog("Matchmaking")
        if not mm_cog:
            return
        all_rooms = mm_cog.rooms
        
        for cat_id, channel_id in CATEGORY_TO_ROOM_CHANNEL.items():
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            rooms_in_cat = {
                rid: info for rid, info in all_rooms.items()
                if info.get("category_id") == cat_id
            }

            lines: List[str] = []
            if not rooms_in_cat:
                lines.append("**No hay salas activas en esta categoría**")
            else:
                rooms_list: List[Tuple[int, int, List[Tuple[discord.Member, int]]]] = []
                for rid, info in rooms_in_cat.items():
                    pdata: List[Tuple[discord.Member, int]] = []
                    mmr_vals: List[int] = []
                    for mem in info.get('players', []):
                        if isinstance(mem, discord.Member):
                            mmr, _ = await mm_cog.fetch_player(mem.id)
                            pdata.append((mem, mmr))
                            mmr_vals.append(mmr)
                    avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0
                    rooms_list.append((rid, avg, pdata))

                rooms_list.sort(key=lambda x: x[1], reverse=True)
                for rid, avg, pdata in rooms_list:
                    lines.append(f"**Room {rid}** – Avg MMR: **{avg}**")
                    for mem, mmr in pdata:
                        lines.append(f"• {mem.display_name} ({mmr})")
                    lines.append("")

            lines.append(f"**Last Updated:** <t:{int(time.time())}:R>")
            content = "\n".join(lines)

            msg = self.posted_messages.get(channel_id)
            if msg is None:
                sent = await channel.send(content)
                self.posted_messages[channel_id] = sent
            else:
                await msg.edit(content=content)

    async def on_room_finished(self, room_id: int):
        await asyncio.sleep(10)
        mm_cog = self.bot.get_cog("Matchmaking")
        if mm_cog and room_id in mm_cog.rooms:
            del mm_cog.rooms[room_id]
            await self._do_update()

async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))
    await bot.add_cog(Rooms(bot))
    # Sincronizar comandos slash en tu servidor de prueba
    bot.tree.copy_global_to(guild=discord.Object(id=YOUR_GUILD_ID))
    await bot.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))
    print("✅ Cogs cargados: Matchmaking, Rooms")
