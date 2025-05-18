import os
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import re
import asyncio
import random

# Configuración
DB_PATH = 'matchmaking.db'
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
JOIN_CHANNEL_NAME = 'join'

# Regex para !submit5
ENTRY_RE = re.compile(r"^<@!?(?P<id>\d+)>\s*\[(?P<cc>\w{2})\]\s*(?P<stats>\d+,\d+,\d+,\d+,\d+)$")

# Convertir código país a emoji bandera
def country_flag(code: str) -> str:
    code = code.upper()
    if len(code) != 2:
        return ''
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

class Matchmaking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # rooms: {rid: {'players': [Member,...], 'thread': Thread}}
        self.rooms: dict[int, dict] = {}

    async def cog_load(self):
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                mmr INTEGER DEFAULT 0,
                role TEXT DEFAULT 'Placement'
            );
            """
        )
        await self.db.commit()

    async def cog_unload(self):
        await self.db.close()

    async def fetch_player(self, user_id: int) -> tuple[int,str]:
        cursor = await self.db.execute(
            "SELECT mmr, role FROM players WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return row
        # insertar nuevo
        await self.db.execute(
            "INSERT INTO players (user_id, mmr, role) VALUES (?, ?, ?)",
            (user_id, 0, 'Placement')
        )
        await self.db.commit()
        return (0, 'Placement')

    async def ensure_join_channel(self, guild: discord.Guild) -> discord.TextChannel:
        for ch in guild.text_channels:
            if ch.name == JOIN_CHANNEL_NAME:
                return ch
        return await guild.create_text_channel(JOIN_CHANNEL_NAME)

    async def sort_and_rename_rooms(self, guild: discord.Guild):
        # Ordenar salas por MMR promedio descendente
        avgs = []  # list of (rid, avg)
        for rid, info in self.rooms.items():
            members = info['players']
            if not members:
                continue
            total = 0
            for m in members:
                m_mmr, _ = await self.fetch_player(m.id)
                total += m_mmr
            avgs.append((rid, total/len(members)))
        avgs.sort(key=lambda x: x[1], reverse=True)
        new_rooms = {}
        for idx, (old_rid, _) in enumerate(avgs, start=1):
            info = self.rooms[old_rid]
            try:
                await info['thread'].edit(name=f"sala-{idx}")
            except:
                pass
            new_rooms[idx] = info
        self.rooms = new_rooms

    async def launch_level_poll(self, room_info: dict):
        thread: discord.Thread = room_info['thread']
        players = room_info['players']
        # contar roles en Discord (incluye Placement si existe)
        counts = {'Placement':0, 'Bronze':0, 'Gold':0, 'Diamond':0}
        for m in players:
            if discord.utils.get(m.roles, name='Placement'):
                counts['Placement'] += 1
            elif discord.utils.get(m.roles, name='Bronze'):
                counts['Bronze'] += 1
            elif discord.utils.get(m.roles, name='Gold'):
                counts['Gold'] += 1
            elif discord.utils.get(m.roles, name='Diamond'):
                counts['Diamond'] += 1
        present = [r for r,c in counts.items() if c>0]
        # opciones de nivel según presentes
        if len(present) == 1:
            role = present[0]
            if role == 'Placement': opts = [29,30,31]
            elif role == 'Bronze': opts = [25,26,27]
            elif role == 'Gold': opts = [28,29,30]
            else: opts = [31,32,33]
        else:
            # dos o tres rangos → opciones fijas 28-30
            opts = [28,29,30]
        # enviar encuesta
        text = "Elijan nivel de dificultad (reacciona 1️⃣ 2️⃣ 3️⃣):\n"
        for i,lvl in enumerate(opts,1): text += f"{i}. Nivel {lvl}\n"
        msg = await thread.send(text)
        for e in ['1️⃣','2️⃣','3️⃣'][:len(opts)]: await msg.add_reaction(e)
        # contar votos
        await asyncio.sleep(30)
        msg = await thread.fetch_message(msg.id)
        votes = {opts[i]: r.count-1 for i,r in enumerate(msg.reactions) if i<len(opts)}
        maxv = max(votes.values())
        winners = [lvl for lvl,v in votes.items() if v==maxv]
        if len(winners)>1:
            await asyncio.sleep(30)
            msg = await thread.fetch_message(msg.id)
            votes = {opts[i]: r.count-1 for i,r in enumerate(msg.reactions) if i<len(opts)}
            maxv = max(votes.values())
            winners = [lvl for lvl,v in votes.items() if v==maxv]
            chosen = random.choice(winners)
        else:
            chosen = winners[0]
        await thread.send(f"✅ Dificultad elegida: Nivel {chosen}")
        # borrar hilo tras 30 minutos
        await asyncio.sleep(1800)
        try:
            await thread.delete()
        except:
            pass
        await self.sort_and_rename_rooms(thread.guild)

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="c", description="Unirse a una sala")
    async def join_room(self, interaction: discord.Interaction):
        if interaction.channel.name != JOIN_CHANNEL_NAME:
            return await interaction.response.send_message(f"❌ Usa /c en #{JOIN_CHANNEL_NAME}", ephemeral=True)
        member = interaction.user
        mmr_val,_ = await self.fetch_player(member.id)
        # buscar sala con espacio
        best_rid,best_score = None,float('inf')
        for rid, info in self.rooms.items():
            if len(info['players']) >= 5:
                continue
            # calcular lista de MMR sin usar índices en coroutine
            vals = []
            for m in info['players']:
                m_mmr, _ = await self.fetch_player(m.id)
                vals.append(m_mmr)
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            diff = abs(avg - mmr_val)
            if diff < best_score:
                best_rid, best_score = rid, diff

        if best_rid is None:
            new_id=max(self.rooms.keys(),default=0)+1
            ch=await self.ensure_join_channel(interaction.guild)
            thread=await ch.create_thread(name=f"sala-{new_id}",auto_archive_duration=60,type=discord.ChannelType.public_thread)
            self.rooms[new_id]={'players':[],'thread':thread}
            best_rid=new_id
        room=self.rooms[best_rid]
        if member in room['players']:
            return await interaction.response.send_message("❌ Ya estás en esa sala.", ephemeral=True)
        room['players'].append(member)
        await interaction.response.send_message(f"✅ Te uniste a sala-{best_rid} — MMR {mmr_val}")
        await room['thread'].add_user(member)
        await room['thread'].send(f"{member.display_name} se unió — MMR {mmr_val}")
        await self.sort_and_rename_rooms(interaction.guild)
        if len(room['players'])==5:
            asyncio.create_task(self.launch_level_poll(room))

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="d", description="Salir de la sala")
    async def leave_room(self, interaction: discord.Interaction):
        member=interaction.user
        for rid,info in list(self.rooms.items()):
            if member in info['players']:
                info['players'].remove(member)
                await info['thread'].send(f"{member.display_name} salió.")
                await info['thread'].remove_user(member)
                if not info['players']:
                    try: await info['thread'].edit(archived=True,locked=True)
                    except: pass
                    self.rooms.pop(rid)
                await interaction.response.send_message(f"🚪 Saliste de sala-{rid}.")
                await self.sort_and_rename_rooms(interaction.guild)
                return
        await interaction.response.send_message("❌ No estás en ninguna sala.", ephemeral=True)

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="mmr", description="Muestra tu MMR actual")
    async def mmr_self(self, interaction: discord.Interaction):
        mmr_val, role = await self.fetch_player(interaction.user.id)
        name = interaction.user.display_name
        if role == 'Placement':
            await interaction.response.send_message(f"{name} está en estado Placement y aún no tiene MMR asignado.")
        else:
            await interaction.response.send_message(f"{name} tiene {mmr_val} MMR y rango {role}.")


    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="mmr_user", description="Muestra el MMR de otro jugador")
    async def mmr_user(self, interaction: discord.Interaction, user: discord.Member):
        mmr_val, role = await self.fetch_player(user.id)
        name = user.display_name
        if role == 'Placement':
            await interaction.response.send_message(f"{name} está en estado Placement y aún no tiene MMR asignado.")
        else:
            await interaction.response.send_message(f"{name} tiene {mmr_val} MMR y rango {role}.")


    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="top10", description="Top 10 jugadores por MMR")
    async def top10_slash(self, interaction: discord.Interaction):
        cur=await self.db.execute("SELECT user_id,mmr FROM players ORDER BY mmr DESC LIMIT 10")
        top=await cur.fetchall()
        if not top: return await interaction.response.send_message("Aún no hay jugadores con MMR definido.")
        lines=[]
        for i,(uid,mmr_val) in enumerate(top,1):
            mem=interaction.guild.get_member(uid)
            nm=mem.display_name if mem else f"ID {uid}"
            lines.append(f"{i}. {nm} — {mmr_val} MMR")
        await interaction.response.send_message("\n".join(lines))

    @commands.command(name="submit5")
    async def submit5(self, ctx: commands.Context, *, block: str):
        lines=[l.strip() for l in block.split("\n") if l.strip()]
        if len(lines)!=5: return await ctx.send("⚠️ Debes enviar 5 líneas: @user [CC] p,g,gd,b,m")
        room=next((r for r in self.rooms.values() if r['thread'].id==ctx.channel.id),None)
        if not room: return await ctx.send("❌ Usa este comando dentro de un hilo de sala.")
        players_list=[]
        for ln in lines:
            m=ENTRY_RE.match(ln)
            if not m: return await ctx.send(f"❌ Formato incorrecto: `{ln}`")
            uid=int(m.group('id'))
            cc=m.group('cc')
            stats=list(map(int,m.group('stats').split(',')))
            member=ctx.guild.get_member(uid) or await ctx.guild.fetch_member(uid)
            weights=[5,3,2,1,0]
            total=sum(s*w for s,w in zip(stats,weights))
            old,db_role=await self.fetch_player(uid)
            placement_bonus=None
            bonus=old
            if old==0 and db_role=='Placement':
                non_perfect=sum(stats[1:])
                if non_perfect<=15:
                    bonus=2000; placement_bonus="(+2000 Placement Diamond)"
                elif non_perfect<=50:
                    bonus=1000; placement_bonus="(+1000 Placement Gold)"
                else:
                    bonus=0; placement_bonus="(+0 Placement Bronze)"
                await self.db.execute("UPDATE players SET mmr=? WHERE user_id=?",(bonus,uid))
            players_list.append({'member':member,'country':cc,'total':total,'old':bonus,'placement_bonus':placement_bonus})
        # ordenar
        players_list.sort(key=lambda x:x['total'],reverse=True)
        avg=sum(p['old'] for p in players_list)/5
        unit=max(1,int(avg//10))
        results=[]
        for idx,p in enumerate(players_list,1):
            mu={1:3,2:2,3:0.5,4:-1,5:-2}[idx]
            delta=int(mu*unit)
            new = p['old'] + delta
            role_name='Bronze' if new<1000 else 'Gold' if new<2000 else 'Diamond'
            await self.db.execute("UPDATE players SET mmr=?,role=? WHERE user_id=?",(new,role_name,p['member'].id))
            role_obj=discord.utils.get(ctx.guild.roles,name=role_name)
            if role_obj:
                await p['member'].remove_roles(*[r for r in p['member'].roles if r.name in {'Bronze','Gold','Diamond'}])
                await p['member'].add_roles(role_obj)
            results.append({'idx':idx,'member':p['member'],'country':p['country'],'total':p['total'],'old':p['old'],'delta':delta,'new':new,'bonus':p['placement_bonus']})
        await self.db.commit()
        # construir mensaje
        lines_out=["🏆 Resultados de ¡submit5! 🏆\n"]
        medals={1:'🥇',2:'🥈',3:'🥉'}
        for r in results:
            if r['bonus']:
                lines_out.append(r['bonus'])
            med=medals.get(r['idx'], '')
            flag=country_flag(r['country'])
            sign='+' if r['delta']>=0 else ''
            lines_out.append(f"{med} {flag} {r['member'].mention}")
            lines_out.append(f"• Puntos Totales: {r['total']}")
            lines_out.append(f"• MMR: {r['old']}{sign}{r['delta']} = {r['new']}\n")
        await ctx.send("\n".join(lines_out))

async def setup(bot: commands.Bot):
    await bot.add_cog(Matchmaking(bot))