from discord.ext import commands

class Placement(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players_in_placement = set()  # Mantiene un registro de los jugadores en colocación

    @commands.command()
    async def start_placement(self, ctx):
        """Inicia el estado de colocación para un jugador."""
        if ctx.author.id in self.players_in_placement:
            await ctx.send(f"{ctx.author.mention}, ya estás en estado de colocación.")
        else:
            self.players_in_placement.add(ctx.author.id)
            await ctx.send(f"{ctx.author.mention}, ahora estás en estado de colocación.")

    @commands.command()
    async def end_placement(self, ctx):
        """Finaliza el estado de colocación para un jugador."""
        if ctx.author.id in self.players_in_placement:
            self.players_in_placement.remove(ctx.author.id)
            await ctx.send(f"{ctx.author.mention}, has finalizado tu estado de colocación.")
        else:
            await ctx.send(f"{ctx.author.mention}, no estás en estado de colocación.")

    @commands.command()
    async def is_placement(self, ctx):
        """Verifica si el jugador está en estado de colocación."""
        if ctx.author.id in self.players_in_placement:
            await ctx.send(f"{ctx.author.mention}, estás en estado de colocación.")
        else:
            await ctx.send(f"{ctx.author.mention}, no estás en estado de colocación.")

async def setup(bot):
    await bot.add_cog(Placement(bot))
