import discord
from discord.ext import commands
from discord import app_commands

class MMR(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Usamos un diccionario para almacenar el MMR de los usuarios
        self.data = {}

    @app_commands.command(name="mmr", description="Muestra el MMR de un jugador.")
    async def mmr(self, interaction: discord.Interaction, user: discord.User = None):
        user = user or interaction.user  # Si no se pasa un usuario, toma al que ejecutó el comando
        user_data = self.data.get(str(user.id))  # Buscamos el MMR del jugador

        if user_data is None:
            await interaction.response.send_message(f"{user.name} no tiene un MMR registrado.")
        else:
            mmr = user_data.get("mmr", 0)  # MMR por defecto 0 si no está definido
            await interaction.response.send_message(f"{user.name} tiene un MMR de {mmr}.")

    @app_commands.command(name="top10", description="Muestra los 10 mejores jugadores por MMR.")
    async def top10(self, interaction: discord.Interaction):
        # Filtramos los usuarios registrados en self.data y los ordenamos por MMR
        sorted_users = sorted(self.data.items(), key=lambda x: x[1]["mmr"], reverse=True)

        # Extraemos los primeros 10 jugadores
        top_10 = sorted_users[:10]

        # Si hay jugadores, formateamos la respuesta
        if top_10:
            response = "Top 10 jugadores:\n"
            for i, (user_id, user_data) in enumerate(top_10, 1):
                user = self.bot.get_user(int(user_id)) or "Desconocido"
                mmr = user_data["mmr"]
                response += f"{i}. {user} - MMR: {mmr}\n"
            await interaction.response.send_message(response)
        else:
            await interaction.response.send_message("No hay jugadores registrados aún.")

async def setup(bot: commands.Bot):
    await bot.add_cog(MMR(bot))


