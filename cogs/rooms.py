import discord, time, asyncio
from discord.ext import commands, tasks
from discord import app_commands

# IDs de los canales join permitidos
JOIN_CHANNELS = {
    1371307353437110282,  # join de pjsk-queue
    1378215330979254402,  # join de jp-pjsk
}

# Mapea categoría → canal rooms
CATEGORY_TO_ROOM_CHANNEL: dict[int, int] = {
    1371306302671687710: 1371307831176728706,  # pjsk-queue → rooms A
    1371951461612912802: 1388515368934309978,  # jp-pjsk  → rooms B
}

def is_join(channel: discord.abc.GuildChannel) -> bool:
    return isinstance(channel, discord.TextChannel) and channel.id in JOIN_CHANNELS

class Matchmaking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # { rooms_channel_id: { room_id: {"players": [...]} } }
        self.rooms: dict[int, dict[int, dict]] = {}

    @app_commands.command(name="c", description="Crear sala por MMR")
    async def cmd_c(self, interaction: discord.Interaction):
        chan = interaction.channel
        # 1) Validar que sea un join
        if not is_join(chan):
            return await interaction.response.send_message(
                "❌ `/c` sólo en canales de **join**.", ephemeral=True
            )

        # 2) Obtener el rooms_channel_id de la categoría
        cat_id = chan.category_id
        if cat_id not in CATEGORY_TO_ROOM_CHANNEL:
            return await interaction.response.send_message(
                "❌ Categoría no habilitada.", ephemeral=True
            )
        rooms_chan_id = CATEGORY_TO_ROOM_CHANNEL[cat_id]

        # 3) Crear la sala **dentro del dict de ese canal**,
        #    de modo que el contador sea independiente.
        bucket = self.rooms.setdefault(rooms_chan_id, {})
        room_id = max(bucket.keys(), default=0) + 1
        bucket[room_id] = {"players": [interaction.user]}

        # 4) Disparar actualización
        self.bot.dispatch("room_updated", rooms_chan_id)

        # 5) Feedback al jugador
        await interaction.response.send_message(
            f"🔔 Sala **{room_id}** creada. Revisa el estado en <#{rooms_chan_id}>.",
            ephemeral=True
        )

    @app_commands.command(name="d", description="Abandonar sala")
    async def cmd_d(self, interaction: discord.Interaction):
        chan = interaction.channel
        if not is_join(chan):
            return await interaction.response.send_message(
                "❌ `/d` sólo en canales de **join**.", ephemeral=True
            )

        cat_id = chan.category_id
        rooms_chan_id = CATEGORY_TO_ROOM_CHANNEL.get(cat_id)
        if not rooms_chan_id or rooms_chan_id not in self.rooms:
            return await interaction.response.send_message(
                "❌ No hay salas activas en esta categoría.", ephemeral=True
            )

        bucket = self.rooms[rooms_chan_id]
        # Busca la sala donde está el usuario
        for rid, info in list(bucket.items()):
            if interaction.user in info["players"]:
                info["players"].remove(interaction.user)
                # Si la sala quedó vacía, elimínala y renumera después
                if not info["players"]:
                    del bucket[rid]
                self.bot.dispatch("room_updated", rooms_chan_id)
                return await interaction.response.send_message(
                    f"🛑 Te has salido de la Sala **{rid}**.", ephemeral=True
                )

        await interaction.response.send_message(
            "❌ No estabas en ninguna sala activa.", ephemeral=True
        )


class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guardamos por canal de rooms el último mensaje enviado
        self.posted_messages: dict[int, discord.Message] = {}
        self.update_rooms.start()
        bot.add_listener(self.on_room_updated, "room_updated")

    @tasks.loop(seconds=15.0)
    async def update_rooms(self):
        await self._do_update()

    @update_rooms.before_loop
    async def before_update(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, rooms_chan_id: int):
        # Cuando un Matchmaking dispare el evento con el canal,
        # forzamos una actualización
        await self._do_update(rooms_chan_id)

    async def _do_update(self, specific_chan: int | None = None):
        mm = self.bot.get_cog("Matchmaking")
        if not mm:
            return

        # Determina qué canales actualizar: uno solo o todos
        channels = [specific_chan] if specific_chan else mm.rooms.keys()

        for chan_id in channels:
            bucket = mm.rooms.get(chan_id, {})
            channel = self.bot.get_channel(chan_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            # Construcción de contenido
            lines: list[str] = []
            if not bucket:
                lines.append("**No hay salas activas en esta categoría**")
            else:
                # Para cada sala, en orden (1,2,3…)
                for rid in sorted(bucket):
                    info = bucket[rid]
                    # Calcula avg MMR (similares a tu código original)
                    pdata, mmr_vals = [], []
                    for mem in info["players"]:
                        try:
                            mmr, _ = await mm.fetch_player(mem.id)
                        except:
                            mmr = 0
                        pdata.append((mem, mmr))
                        mmr_vals.append(mmr)
                    avg = sum(mmr_vals) // len(mmr_vals) if mmr_vals else 0

                    lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                    for mem, val in pdata:
                        lines.append(f"{mem.display_name} ({val})")
                    lines.append("")

            lines.append(f"**Last Updated:** <t:{int(time.time())}:R>")
            content = "\n".join(lines)

            # Envía o edita
            existing = self.posted_messages.get(chan_id)
            if existing:
                await existing.edit(content=content)
            else:
                sent = await channel.send(content)
                self.posted_messages[chan_id] = sent


async def setup(bot: commands.Bot):
    bot.tree.add_command(Matchmaking.cmd_c)
    bot.tree.add_command(Matchmaking.cmd_d)
    await bot.add_cog(Matchmaking(bot))
    await bot.add_cog(Rooms(bot))
    await bot.tree.sync()
