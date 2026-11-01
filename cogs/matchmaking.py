import asyncio
from datetime import datetime, timezone
import json
import logging
import random
import weakref
import discord
from discord import app_commands
from discord.ext import commands, tasks
from lounge.rules import RANKS, song_range
from lounge.songs import SongCatalog

log = logging.getLogger(__name__)


def country_flag(code):
    return (
        "".join(chr(ord(c) + 127397) for c in code.upper())
        if len(code) == 2 and code.isalpha()
        else ""
    )


class SongPollView(discord.ui.View):
    def __init__(self, cog, room_id, songs):
        super().__init__(timeout=None)
        self.cog = cog
        self.room_id = room_id
        select = discord.ui.Select(
            custom_id=f"pjsk:poll:{room_id}",
            placeholder="👆 Tu voto (puedes cambiar antes de 1 min)",
            options=[
                discord.SelectOption(
                    label=f"{title} (Lv {level} {diff})"[:100], value=str(i)
                )
                for i, (title, level, diff) in enumerate(songs)
            ],
        )

        async def callback(inter):
            await inter.response.defer(ephemeral=True, thinking=True)
            try:
                choice = await cog.bot.repo.vote(
                    room_id, inter.user.id, int(select.values[0])
                )
                await inter.followup.send(
                    f"✅ Tu voto por **{choice[0]}** ha sido registrado.",
                    ephemeral=True,
                )
            except ValueError as exc:
                await inter.followup.send(str(exc), ephemeral=True)

        select.callback = callback
        self.add_item(select)


class DiagnosticPoll(discord.ui.View):
    """The original public debug poll; never writes competitive data."""

    def __init__(self, songs, channel):
        super().__init__(timeout=60)
        self.songs = songs
        self.channel = channel
        self.votes = {}
        self.vote_map = {}
        self.message = None
        select = discord.ui.Select(
            options=[
                discord.SelectOption(label=f"{t} (Lv {l} {d})"[:100], value=str(i))
                for i, (t, l, d) in enumerate(songs)
            ]
        )

        async def vote(inter):
            choice = int(select.values[0])
            uid = inter.user.id
            if uid in self.vote_map:
                self.votes[self.vote_map[uid]] -= 1
            self.vote_map[uid] = choice
            self.votes[choice] = self.votes.get(choice, 0) + 1
            await inter.response.send_message(
                f"✅ Tu voto por **{songs[choice][0]}** ha sido registrado.",
                ephemeral=True,
            )

        select.callback = vote
        self.add_item(select)

    async def on_timeout(self):
        try:
            if self.message:
                await self.message.edit(view=None)
            if not self.votes:
                await self.channel.send(
                    "⚠️ No se registraron votos en el tiempo establecido."
                )
            else:
                maximum = max(self.votes.values())
                winners = [i for i, n in self.votes.items() if n == maximum]
                title, level, diff = self.songs[random.choice(winners)]
                await self.channel.send(
                    f"🏆 **Resultado de la votación** 🏆\nLa canción ganadora es **{title}** (Lv {level}, {diff}) con **{maximum} votos**."
                )
        except discord.HTTPException:
            log.exception("diagnostic_poll_delivery_failed")
        finally:
            self.stop()


class Matchmaking(commands.Cog):
    _range_for_counts = staticmethod(song_range)

    def __init__(self, bot):
        self.bot = bot
        self.catalog = SongCatalog(bot.db)
        self.room_locks = weakref.WeakValueDictionary()
        self.member_locks = weakref.WeakValueDictionary()
        self.confirmed_members = set()
        self.views = {}

    async def cog_load(self):
        await self.catalog.load()
        self.refresh_songs.start()
        self.maintenance.start()

    async def cog_unload(self):
        self.refresh_songs.cancel()
        self.maintenance.cancel()
        for view in self.views.values():
            view.stop()

    @tasks.loop(hours=6)
    async def refresh_songs(self):
        try:
            await self.catalog.refresh()
        except Exception:
            log.exception(
                "catalog_refresh_failed cached_charts=%s", len(self.catalog.rows)
            )

    @refresh_songs.before_loop
    async def before_songs(self):
        await self.bot.wait_until_ready()

    async def thread(self, room):
        if not room["thread_id"]:
            return None
        thread = self.bot.get_channel(
            room["thread_id"]
        ) or await self.bot.fetch_channel(room["thread_id"])
        if not isinstance(thread, discord.Thread):
            raise ValueError("El canal persistido no es un thread")
        if (
            thread.guild.id != room["guild_id"]
            or str(thread.parent_id) != room["queue_id"]
        ):
            raise ValueError("Thread fuera de la cola configurada")
        return thread

    async def provision(self, room, channel):
        lock = self.room_locks.setdefault(room["id"], asyncio.Lock())
        async with lock:
            rooms = await self.bot.repo.rooms()
            current = next((r for r in rooms if r["id"] == room["id"]), None)
            if not current or current["status"] == "needs_attention":
                raise ValueError("Sala pendiente de reconciliación administrativa")
            thread = await self.thread(current)
            if thread:
                return thread
            try:
                thread = await channel.create_thread(
                    name=f"sala-{current['display_number']}",
                    auto_archive_duration=60,
                    type=discord.ChannelType.public_thread,
                )
                await self.bot.repo.attach_thread(room["id"], thread.id)
                return thread
            except Exception:
                # An HTTP timeout may have created a thread remotely. Never blindly create it twice.
                await self.bot.repo.issue(
                    room["id"], "thread_creation_failed_or_ambiguous"
                )
                raise

    async def launch_song_poll(self, room, thread):
        if len(room["players"]) != 5 or room["status"] != "open":
            return
        async with self.bot.db.pool.acquire() as c:
            roles = await c.fetch(
                "SELECT role FROM season_players WHERE season_id=$1 AND user_id=ANY($2::bigint[])",
                room["season_id"],
                room["players"],
            )
        counts = {rank: sum(p["role"] == rank for p in roles) for rank in RANKS}
        low, high = song_range(counts)
        songs = self.catalog.pick(low, high)
        if not songs:
            await self.bot.repo.issue(room["id"], "catalog_empty_for_rank_range")
            await thread.send(
                "⚠️ No hay canciones en ese rango. Se requiere revisar el catálogo."
            )
            return
        if not await self.bot.repo.create_poll(room["id"], songs):
            return
        view = SongPollView(self, room["id"], songs)
        try:
            msg = await thread.send(
                f"🎉 Sala completa · Niveles {high}-{low}\nTienen **1 minuto** para votar la canción:",
                view=view,
            )
            await self.bot.repo.activate_poll(room["id"], msg.id)
            self.views[room["id"]] = view
        except Exception:
            view.stop()
            await self.bot.repo.issue(room["id"], "poll_send_failed_or_ambiguous")
            raise

    async def membership(self, room, thread, user_id, action):
        if action == "add":
            await thread.add_user(discord.Object(id=user_id))
            self.confirmed_members.add((thread.id, user_id))
        else:
            await thread.remove_user(discord.Object(id=user_id))
            self.confirmed_members.discard((thread.id, user_id))
        async with self.bot.db.pool.acquire() as c:
            await c.execute(
                "DELETE FROM thread_jobs WHERE room_id=$1 AND user_id=$2 AND action=$3",
                room["id"],
                user_id,
                action,
            )

    async def membership_jobs(self):
        async with self.bot.db.pool.acquire() as c:
            jobs = await c.fetch(
                "SELECT j.user_id,j.action,j.attempts,r.* FROM thread_jobs j JOIN rooms r ON r.id=j.room_id WHERE j.next_attempt<=now() AND r.thread_id IS NOT NULL ORDER BY j.next_attempt LIMIT 25"
            )
        for job in jobs:
            try:
                if not job["thread_id"]:
                    continue
                if job["action"] == "add":
                    async with self.bot.db.pool.acquire() as c:
                        valid = await c.fetchval(
                            "SELECT 1 FROM room_members WHERE room_id=$1 AND user_id=$2",
                            job["id"],
                            job["user_id"],
                        )
                        if not valid:
                            await c.execute(
                                "DELETE FROM thread_jobs WHERE room_id=$1 AND user_id=$2 AND action='add'",
                                job["id"],
                                job["user_id"],
                            )
                            continue
                thread = await self.thread(job)
                if thread.archived:
                    # Archival supersedes membership work; never reopen an expired room.
                    async with self.bot.db.pool.acquire() as c:
                        await c.execute(
                            "DELETE FROM thread_jobs WHERE room_id=$1", job["id"]
                        )
                    continue
                await self.membership(job, thread, job["user_id"], job["action"])
            except (discord.HTTPException, ValueError) as exc:
                code = (
                    f"discord_{exc.status}"
                    if isinstance(exc, discord.HTTPException)
                    else "invalid_thread"
                )
                delay = (
                    min(3600, 5 * 2 ** min(job["attempts"], 10)) + random.random() * 5
                )
                async with self.bot.db.pool.acquire() as c:
                    await c.execute(
                        "UPDATE thread_jobs SET attempts=attempts+1,next_attempt=now()+$3*interval '1 second',last_error=$4 WHERE room_id=$1 AND user_id=$2",
                        job["id"],
                        job["user_id"],
                        delay,
                        code,
                    )
                log.warning(
                    "thread_membership_retry room_id=%s reason=%s", job["id"], code
                )

    async def cancelled_poll_jobs(self):
        async with self.bot.db.pool.acquire() as c:
            jobs = await c.fetch(
                "SELECT p.message_id,r.* FROM polls p JOIN rooms r ON r.id=p.room_id WHERE p.status='cancelled' AND NOT p.announced AND p.message_id IS NOT NULL LIMIT 25"
            )
        for job in jobs:
            try:
                thread = await self.thread(job)
                if thread and not thread.archived:
                    message = await thread.fetch_message(job["message_id"])
                    await message.edit(view=None)
                view = self.views.pop(job["id"], None)
                if view:
                    view.stop()
                async with self.bot.db.pool.acquire() as c:
                    await c.execute(
                        "UPDATE polls SET announced=true WHERE room_id=$1", job["id"]
                    )
            except discord.NotFound:
                async with self.bot.db.pool.acquire() as c:
                    await c.execute(
                        "UPDATE polls SET announced=true WHERE room_id=$1", job["id"]
                    )
                log.warning("cancelled_poll_message_missing room_id=%s", job["id"])
            except discord.HTTPException:
                log.exception("cancelled_poll_cleanup_retry room_id=%s", job["id"])

    @tasks.loop(seconds=5)
    async def maintenance(self):
        try:
            await self.membership_jobs()
            await self.cancelled_poll_jobs()
            async with self.bot.db.pool.acquire() as c:
                archival = [
                    dict(r)
                    for r in await c.fetch(
                        "SELECT * FROM rooms WHERE status='closed' AND issue='archive_pending'"
                    )
                ]
            for room in archival:
                try:
                    thread = await self.thread(room)
                    if thread and not thread.archived:
                        await thread.edit(archived=True, locked=True)
                    async with self.bot.db.pool.acquire() as c:
                        await c.execute(
                            "UPDATE rooms SET issue=NULL WHERE id=$1", room["id"]
                        )
                except discord.NotFound:
                    async with self.bot.db.pool.acquire() as c:
                        await c.execute(
                            "UPDATE rooms SET issue='thread_missing_on_archive' WHERE id=$1",
                            room["id"],
                        )
                except discord.HTTPException:
                    log.exception("room_archive_retry room_id=%s", room["id"])
            rooms = await self.bot.repo.rooms()
            active_threads = {r["thread_id"] for r in rooms}
            self.confirmed_members = {
                pair for pair in self.confirmed_members if pair[0] in active_threads
            }
            for room in rooms:
                try:
                    if room["status"] == "needs_attention":
                        continue
                    if not room["thread_id"]:
                        if (
                            datetime.now(timezone.utc) - room["created_at"]
                        ).total_seconds() > 90:
                            await self.bot.repo.issue(
                                room["id"], "restart_during_thread_creation"
                            )
                        continue
                    thread = await self.thread(room)
                    if thread.archived:
                        async with self.bot.db.competitive() as (c, s):
                            await c.execute(
                                "UPDATE rooms SET status='closed',issue='thread_archived',updated_at=now() WHERE id=$1",
                                room["id"],
                            )
                            await c.execute(
                                "DELETE FROM room_members WHERE room_id=$1", room["id"]
                            )
                            await c.execute(
                                "UPDATE polls SET status='cancelled' WHERE room_id=$1 AND status IN('sending','active')",
                                room["id"],
                            )
                        continue
                    name = f"sala-{room['display_number']}"
                    if thread.name != name:
                        await thread.edit(name=name)
                    for uid in room["players"]:
                        if (
                            thread.get_member(uid) is None
                            and (thread.id, uid) not in self.confirmed_members
                        ):
                            await thread.add_user(discord.Object(id=uid))
                            self.confirmed_members.add((thread.id, uid))
                    async with self.bot.db.pool.acquire() as c:
                        poll = await c.fetchrow(
                            "SELECT * FROM polls WHERE room_id=$1", room["id"]
                        )
                    if poll:
                        if poll["status"] == "sending" and not poll["message_id"]:
                            if (
                                datetime.now(timezone.utc) - room["updated_at"]
                            ).total_seconds() > 30:
                                await self.bot.repo.issue(
                                    room["id"],
                                    "poll_delivery_unknown_manual_reconciliation",
                                )
                            continue
                        if poll["status"] == "active" and room["id"] not in self.views:
                            view = SongPollView(
                                self, room["id"], json.loads(poll["options"])
                            )
                            self.bot.add_view(view, message_id=poll["message_id"])
                            self.views[room["id"]] = view
                        if poll["status"] in ("active", "closed") and poll[
                            "expires_at"
                        ] <= datetime.now(timezone.utc):
                            await self.finish_poll(room, thread)
                    else:
                        await self.launch_song_poll(room, thread)
                except (discord.NotFound, ValueError):
                    await self.bot.repo.issue(
                        room["id"], "thread_or_message_missing_or_invalid"
                    )
                    log.exception("room_recovery_failed room_id=%s", room["id"])
                except discord.HTTPException as exc:
                    log.warning(
                        "discord_room_retry room_id=%s status=%s",
                        room["id"],
                        exc.status,
                    )
        except Exception:
            log.exception("room_maintenance_failed")

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.bot.wait_until_ready()

    async def finish_poll(self, room, thread):
        poll = await self.bot.repo.close_poll(room["id"], random)
        if not poll or poll["announced"]:
            return
        view = self.views.pop(room["id"], None)
        if view:
            for child in view.children:
                child.disabled = True
        if poll["message_id"]:
            message = await thread.fetch_message(poll["message_id"])
            # Edit the original message rather than send another: restart-safe announcement.
            if poll["winner"] is None:
                content = "⚠️ No se registraron votos en el tiempo establecido."
            else:
                title, level, diff = json.loads(poll["options"])[poll["winner"]]
                async with self.bot.db.pool.acquire() as c:
                    count = await c.fetchval(
                        "SELECT count(*) FROM poll_votes WHERE room_id=$1 AND choice=$2",
                        room["id"],
                        poll["winner"],
                    )
                content = f"🏆 **Resultado de la votación** 🏆\nLa canción ganadora es **{title}** (Lv {level}, {diff}) con **{count} votos**."
            await message.edit(content=content, view=None)
        if view:
            view.stop()
        async with self.bot.db.pool.acquire() as c:
            await c.execute(
                "UPDATE polls SET announced=true WHERE room_id=$1", room["id"]
            )

    @app_commands.command(name="c", description="Unirse a una sala")
    async def join_room(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        lock = self.member_locks.setdefault(
            (interaction.guild_id, interaction.user.id), asyncio.Lock()
        )
        async with lock:
            await self.join_after_ack(interaction)

    async def join_after_ack(self, interaction):
        permissions = getattr(interaction, "app_permissions", None)
        if permissions is not None and not (
            permissions.create_public_threads
            and permissions.send_messages_in_threads
            and permissions.view_channel
        ):
            return await interaction.followup.send(
                "❌ El bot necesita View Channel, Create Public Threads y Send Messages in Threads en esta cola.",
                ephemeral=True,
            )
        queue = self.bot.settings.queue_key(
            interaction.guild_id,
            interaction.channel_id,
            getattr(interaction.channel, "name", ""),
        )
        if not queue:
            return await interaction.followup.send(
                "❌ Este comando solo funciona en un canal de cola configurado.",
                ephemeral=True,
            )
        room, mmr = await self.bot.repo.join(
            interaction.guild_id,
            queue,
            interaction.user.id,
            interaction.user.display_name,
        )
        thread = await self.provision(room, interaction.channel)
        await self.membership(room, thread, interaction.user.id, "add")
        await interaction.followup.send(
            f"✅ Te uniste a sala-{room['display_number']} — MMR {mmr}"
        )
        await thread.send(f"{interaction.user.display_name} se unió — MMR {mmr}")

    @app_commands.command(name="d", description="Salir de la sala")
    async def leave_room(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        lock = self.member_locks.setdefault(
            (interaction.guild_id, interaction.user.id), asyncio.Lock()
        )
        async with lock:
            await self.leave_after_ack(interaction)

    async def leave_after_ack(self, interaction):
        room, left = await self.bot.repo.leave(
            interaction.guild_id, interaction.user.id
        )
        await interaction.followup.send(f"🚪 Saliste de sala-{room['display_number']}.")
        thread = await self.thread(room)
        if thread:
            try:
                await thread.send(f"{interaction.user.display_name} salió.")
                await self.membership(room, thread, interaction.user.id, "remove")
                if not left:
                    await thread.edit(archived=True, locked=True)
                    async with self.bot.db.pool.acquire() as c:
                        await c.execute(
                            "UPDATE rooms SET issue=NULL WHERE id=$1", room["id"]
                        )
            except discord.HTTPException:
                log.exception("leave_discord_pending room_id=%s", room["id"])

    async def show_mmr(self, interaction, user):
        await interaction.response.defer(thinking=True)
        p = await self.bot.repo.player(user.id, user.display_name)
        text = f"{user.display_name} tiene {p['mmr']} MMR y rango {p['role']}."
        if p["role"] == "Placement":
            text = f"{user.display_name} está en estado Placement y aún no tiene MMR asignado. (MMR: {p['mmr']})"
        await interaction.followup.send(text)

    @app_commands.command(name="mmr", description="Muestra tu MMR actual")
    async def mmr_self(self, interaction: discord.Interaction):
        await self.show_mmr(interaction, interaction.user)

    @app_commands.command(name="mmr_user", description="Muestra el MMR de otro jugador")
    async def mmr_user(self, interaction: discord.Interaction, user: discord.Member):
        await self.show_mmr(interaction, user)

    @app_commands.command(name="top10", description="Top 10 jugadores por MMR")
    async def top10_slash(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        players = await self.bot.repo.leaderboard()
        await interaction.followup.send(
            "\n".join(
                f"{i}. {p['name'] or str(p['user_id'])} — {p['mmr']} MMR"
                for i, p in enumerate(players, 1)
            )
            or "Aún no hay jugadores con MMR definido."
        )

    @commands.command(name="debug_diffs")
    async def debug_diffs(self, ctx):
        await ctx.send(
            f"Dificultades en el catálogo: {sorted({r['diff'] for r in self.catalog.rows})}"
        )

    @commands.command(name="debug_poll")
    async def debug_poll(self, ctx):
        songs = self.catalog.pick(1, 99)
        await ctx.send(f"🎵 debug_poll sacó {len(songs)} canciones")
        if not songs:
            return await ctx.send("⚠️ No hay canciones en debug_poll.")
        view = DiagnosticPoll(songs, ctx.channel)
        view.message = await ctx.send("🎵 Poll de prueba (60s para votar):", view=view)

    @commands.command(name="submit5")
    async def submit5(self, ctx, *, block: str):
        result, created = await self.bot.repo.submit(
            ctx.channel.id,
            ctx.message.id,
            block,
            ctx.author.id,
            ctx.author.guild_permissions.manage_guild,
        )
        lines = [
            "🏆 Resultados de ¡submit5! 🏆"
            if created
            else "Resultado ya confirmado; MMR sin cambios."
        ]
        for p in result:
            if p["bonus"]:
                lines.append(p["bonus"])
            lines.append(
                f"{country_flag(p['country'])} <@{p['user_id']}> · Puntos Totales: {p['total']} · MMR: {p['old']}{p['delta']:+} = {p['new']}"
            )
        await ctx.send(
            "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot):
    await bot.add_cog(Matchmaking(bot))
