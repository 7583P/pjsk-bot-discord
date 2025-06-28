import discord, time, asyncio
from discord.ext import commands, tasks
from discord import Thread, TextChannel, app_commands

# IDs de los canales #join permitidos
JOIN_CHANNELS = {
    1371307353437110282,  # pjsk-queue → #join
    1378215330979254402,  # jp-pjsk   → #join
}

# Mapea category_id → rooms_channel_id
CATEGORY_TO_ROOM_CHANNEL = {
    1371306302671687710: 1371307831176728706,  # pjsk-queue → #rooms A
    1371951461612912802: 1388515368934309978,  # jp-pjsk   → #rooms B
}

def in_join_or_thread(ch: discord.abc.GuildChannel) -> bool:
    if isinstance(ch, Thread):
        return ch.parent_id in JOIN_CHANNELS
    return isinstance(ch, TextChannel) and ch.id in JOIN_CHANNELS

class Matchmaking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # rooms_per_channel: { rooms_chan_id: { room_id: [Member,…] } }
        self.rooms_per_channel: dict[int, dict[int, list[discord.Member]]] = {}

    @app_commands.command(name="c", description="Crear sala por MMR")
    async def cmd_c(self, interaction: discord.Interaction):
        chan = interaction.channel
        if not in_join_or_thread(chan):
            return await interaction.response.send_message(
                "❌ `/c` sólo en canales **join** o sus hilos.", ephemeral=True
            )

        cat_id = chan.category_id
        rooms_chan = CATEGORY_TO_ROOM_CHANNEL.get(cat_id)
        if not rooms_chan:
            return await interaction.response.send_message(
                "❌ Categoría no habilitada.", ephemeral=True
            )

        bucket = self.rooms_per_channel.setdefault(rooms_chan, {})
        new_id = max(bucket.keys(), default=0) + 1
        bucket[new_id] = [interaction.user]

        self.bot.dispatch("room_updated", rooms_chan)
        await interaction.response.send_message(
            f"🔔 Sala **{new_id}** creada. Estado en <#{rooms_chan}>.",
            ephemeral=True
        )

    @app_commands.command(name="d", description="Salir de sala")
    async def cmd_d(self, interaction: discord.Interaction):
        chan = interaction.channel
        if not in_join_or_thread(chan):
            return await interaction.response.send_message(
                "❌ `/d` sólo en canales **join** o sus hilos.", ephemeral=True
            )

        cat_id = chan.category_id
        rooms_chan = CATEGORY_TO_ROOM_CHANNEL.get(cat_id)
        bucket = self.rooms_per_channel.get(rooms_chan, {})
        for rid, players in list(bucket.items()):
            if interaction.user in players:
                players.remove(interaction.user)
                if not players:
                    del bucket[rid]
                self.bot.dispatch("room_updated", rooms_chan)
                return await interaction.response.send_message(
                    f"🛑 Saliste de la Sala **{rid}**.", ephemeral=True
                )

        await interaction.response.send_message(
            "❌ No estabas en ninguna sala activa.", ephemeral=True
        )

class Rooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.posted: dict[int, discord.Message] = {}
        self.update_loop.start()
        bot.add_listener(self.on_room_updated, "room_updated")

    @tasks.loop(seconds=15.0)
    async def update_loop(self):
        await self._refresh()

    @update_loop.before_loop
    async def wait_ready(self):
        await self.bot.wait_until_ready()

    async def on_room_updated(self, rooms_chan: int):
        await self._refresh(rooms_chan)

    async def _refresh(self, specific_chan: int = None):
        mm = self.bot.get_cog("Matchmaking")
        if not mm:
            return

        targets = [specific_chan] if specific_chan else list(mm.rooms_per_channel)
        for chan_id in targets:
            bucket = mm.rooms_per_channel.get(chan_id, {})
            channel = self.bot.get_channel(chan_id)
            if not isinstance(channel, TextChannel):
                continue

            lines = []
            if not bucket:
                lines.append("**No hay salas activas en esta categoría**")
            else:
                for rid in sorted(bucket):
                    players = bucket[rid]
                    # calcular avg MMR (ejemplo estático 0 si no fetch_player)
                    mmrs = []
                    pdata = []
                    for m in players:
                        try:
                            val, _ = await mm.fetch_player(m.id)
                        except:
                            val = 0
                        pdata.append((m, val))
                        mmrs.append(val)
                    avg = sum(mmrs)//len(mmrs) if mmrs else 0

                    lines.append(f"**Room {rid}** – Average MMR: **{avg}**")
                    for m,val in pdata:
                        lines.append(f"{m.display_name} ({val})")
                    lines.append("")

            lines.append(f"**Last Updated:** <t:{int(time.time())}:R>")
            content = "\n".join(lines)

            msg = self.posted.get(chan_id)
            if msg:
                await msg.edit(content=content)
            else:
                sent = await channel.send(content)
                self.posted[chan_id] = sent

async def setup(bot: commands.Bot):
    bot.tree.add_command(Matchmaking.cmd_c)
    bot.tree.add_command(Matchmaking.cmd_d)
    await bot.add_cog(Matchmaking(bot))
    await bot.add_cog(Rooms(bot))
    await bot.tree.sync()
