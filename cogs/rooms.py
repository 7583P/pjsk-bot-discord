import discord, time, asyncio
from discord.ext import commands, tasks
from discord import app_commands

JOIN_CHANNEL_NAME = "join"
# Tu mapeo existente: categoría → canal rooms
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # Categoría A → Rooms A
    1371951461612912802: 1388515368934309978,  # Categoría B → Rooms B
    # añade aquí más mappings si amplías categorías
}

class Matchmaking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Ahora guardamos también el rooms_channel_id
        self.rooms: dict[int, dict] = {}

    @app_commands.command(name="c", description="Crear sala por MMR")
    async def cmd_c(self, interaction: discord.Interaction):
        chan = interaction.channel
        # 1) Solo en canales llamados "join"
        if not isinstance(chan, discord.TextChannel) or chan.name != JOIN_CHANNEL_NAME:
            await interaction.response.send_message(
                f"❌ Usa `/c` en un canal #{JOIN_CHANNEL_NAME}.", ephemeral=True
            )
            return

        cat_id = chan.category_id
        # 2) La categoría debe estar mapeada
        if cat_id not in CATEGORY_TO_ROOM_CHANNEL:
            await interaction.response.send_message(
                "❌ Esta categoría no está habilitada para emparejamientos.", ephemeral=True
            )
            return

        rooms_chan_id = CATEGORY_TO_ROOM_CHANNEL[cat_id]

        # 3) Crear la sala
        room_id = len(self.rooms) + 1
        self.rooms[room_id] = {
            "players": [interaction.user],
            "rooms_channel_id": rooms_chan_id
        }

        # 4) Fuerza actualización inmediata
        self.bot.dispatch("room_updated", room_id)

        # 5) Confirmación al usuario
        await interaction.response.send_message(
            f"🔔 Sala {room_id} creada. Mira el estado en <#{rooms_chan_id}>.",
            ephemeral=True
        )


class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # store last bot message per rooms_channel_id
        self.posted_messages: dict[int, discord.Message] = {}
        self.update_rooms.start()
        bot.add_listener(self.on_room_updated, "room_updated")

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, room_id: int):
        # cuando se dispare el evento, refrescamos
        await self._do_update()

    async def _do_update(self):
        mm = self.bot.get_cog("Matchmaking")
        if not mm:
            return

        # Agrupamos salas por el canal rooms que les corresponde
        buckets: dict[int, list[tuple[int, dict]]] = {}
        for rid, info in mm.rooms.items():
            chan_id = info["rooms_channel_id"]
            buckets.setdefault(chan_id, []).append((rid, info))

        # Para cada rooms_channel, construimos y enviamos/edítamos el mensaje
        for chan_id, room_list in buckets.items():
            channel = self.bot.get_channel(chan_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            lines: list[str] = []
            # Para cada sala en este canal
            for rid, info in room_list:
                players = info["players"]
                # ... aquí tu lógica de fetch_player / avg MMR ...
                pdata = []
                mmr_vals = []
                for member in players:
                    if isinstance(member, discord.Member):
                        try:
                            mmr_val, _ = await mm.fetch_player(member.id)
                        except:
                            mmr_val = 0
                        pdata.append((member, mmr_val))
                        mmr_vals.append(mmr_val)
                avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0

                lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                for mem, mmr in pdata:
                    lines.append(f"{mem.display_name} ({mmr})")
                lines.append("")

            lines.append(f"**Last Updated:** <t:{int(time.time())}:R>")
            content = "\n".join(lines)

            # Send or edit
            msg = self.posted_messages.get(chan_id)
            if msg is None:
                sent = await channel.send(content)
                self.posted_messages[chan_id] = sent
            else:
                await msg.edit(content=content)

async def setup(bot: commands.Bot):
    bot.tree.add_command(Matchmaking.cmd_c)
    await bot.add_cog(Matchmaking(bot))
    await bot.add_cog(Rooms(bot))
    await bot.tree.sync()
