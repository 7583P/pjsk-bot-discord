"""Lightweight latency instrumentation (no external services, bounded memory).

Records, per operation name, rolling samples and reports p50/p95/p99:
  * ``db.acquire``  – time waiting for a pooled connection
  * ``db.query``    – time spent in SQL round trips
  * ``cmd.<name>``  – Discord interaction: gateway delay, time to first
                      response (ack) and total handling time.
Nothing here alters bot behaviour; every hook is wrapped so metrics can never
break a command.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections import defaultdict, deque
from contextlib import contextmanager

log = logging.getLogger("perf")

WINDOW = 500
import re

# Statements whose effect outlives a transaction on a pooled connection.
_SESSION_STATE = re.compile(
    r"(?is)(^|[;\s])(set\s+(?!local)|reset\s|listen\s|unlisten\s|declare\s|"
    r"discard\s|prepare\s|pg_advisory_(?!xact)\w*lock|pg_advisory_unlock|set_config\s*\([^)]*,\s*(false))"
)

SLOW_MS = {"db.query": 750.0, "db.acquire": 750.0, "ack": 2000.0, "total": 5000.0}

_samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
_counters: dict[str, int] = defaultdict(int)

# Per-command breakdown accumulated through a contextvar (cheap, task-local).
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("perf_current", default=None)


def record(name: str, ms: float) -> None:
    _samples[name].append(ms)
    cur = _current.get()
    if cur is not None:
        cur[name] = cur.get(name, 0.0) + ms
        cur[name + ".n"] = cur.get(name + ".n", 0) + 1


def count(name: str, n: int = 1) -> None:
    _counters[name] += n


def _pct(sorted_vals: list[float], q: float) -> float:
    k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def snapshot() -> dict:
    out: dict = {}
    for name, dq in list(_samples.items()):
        vals = sorted(dq)
        if not vals:
            continue
        out[name] = {
            "n": len(vals),
            "p50_ms": round(_pct(vals, 0.50), 1),
            "p95_ms": round(_pct(vals, 0.95), 1),
            "p99_ms": round(_pct(vals, 0.99), 1),
            "max_ms": round(vals[-1], 1),
        }
    return {"latency": out, "counters": dict(_counters)}


@contextmanager
def command_scope():
    """Collects the DB/ack breakdown of one interaction."""
    bucket: dict = {}
    token = _current.set(bucket)
    try:
        yield bucket
    finally:
        _current.reset(token)


# ---------------------------------------------------------------- database
class _TimedAcquire:
    """Wraps ``pool.acquire()`` so waiting time is measured."""

    def __init__(self, inner):
        self._inner = inner

    def __await__(self):
        async def _run():
            t0 = time.perf_counter()
            try:
                return await self._inner
            finally:
                record("db.acquire", (time.perf_counter() - t0) * 1000)

        return _run().__await__()

    async def __aenter__(self):
        t0 = time.perf_counter()
        try:
            return await self._inner.__aenter__()
        finally:
            record("db.acquire", (time.perf_counter() - t0) * 1000)

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


try:  # pragma: no cover - asyncpg is always present at runtime
    from asyncpg.pool import Pool as _Pool

    class TimedPool(_Pool):
        """Same memory layout as ``Pool`` so an existing pool can be re-classed."""

        __slots__ = ()

        def acquire(self, *args, **kwargs):
            return _TimedAcquire(super().acquire(*args, **kwargs))

except Exception:  # pragma: no cover
    TimedPool = None


def instrument_pool(pool):
    """Time ``pool.acquire`` by re-classing the pool in place (idempotent)."""
    if TimedPool is not None and type(pool).__name__ == "Pool":
        pool.__class__ = TimedPool
    return pool


def timed_connection_class(base):
    """Connection subclass that records query time and skips the per-release
    reset round trip (the bot uses no advisory locks, LISTEN or session SETs;
    open transactions are still rolled back by asyncpg before reuse)."""

    class TimedConnection(base):
        _dirty_session = False

        def get_reset_query(self):
            # Clean connections skip the reset round trip; a connection that ran
            # anything session-scoped gets asyncpg's full reset before reuse.
            if self._dirty_session:
                self._dirty_session = False
                return super().get_reset_query()
            return ""

        def _guard(self, args):
            if args and isinstance(args[0], str) and _SESSION_STATE.search(args[0]):
                self._dirty_session = True

        def add_listener(self, *a, **k):
            self._dirty_session = True
            return super().add_listener(*a, **k)

        async def _timed(self, name, fn, *args, **kwargs):
            t0 = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                ms = (time.perf_counter() - t0) * 1000
                record("db.query", ms)
                if ms > SLOW_MS["db.query"]:
                    log.warning("slow_query ms=%.0f op=%s", ms, name)

        async def fetch(self, *a, **k):
            self._guard(a)
            return await self._timed("fetch", super().fetch, *a, **k)

        async def fetchrow(self, *a, **k):
            self._guard(a)
            return await self._timed("fetchrow", super().fetchrow, *a, **k)

        async def fetchval(self, *a, **k):
            self._guard(a)
            return await self._timed("fetchval", super().fetchval, *a, **k)

        async def execute(self, *a, **k):
            self._guard(a)
            return await self._timed("execute", super().execute, *a, **k)

        async def executemany(self, *a, **k):
            self._guard(a)
            return await self._timed("executemany", super().executemany, *a, **k)

    return TimedConnection


# ----------------------------------------------------------------- discord
def install_discord_hooks(bot) -> None:
    """Records gateway delay, ack latency and total time for slash commands."""
    import discord

    if getattr(bot, "_perf_hooks_installed", False):
        return
    bot._perf_hooks_installed = True

    if not getattr(discord.InteractionResponse, "_perf_patched", False):
        def _wrap(method_name):
            original = getattr(discord.InteractionResponse, method_name)

            async def wrapper(self, *a, **k):
                inter = getattr(self, "_parent", None)
                try:
                    if inter is not None and "t_ack" not in inter.extras:
                        inter.extras["t_ack"] = time.perf_counter()
                except Exception:
                    pass
                return await original(self, *a, **k)

            wrapper.__name__ = method_name
            setattr(discord.InteractionResponse, method_name, wrapper)

        for name in ("defer", "send_message", "edit_message"):
            _wrap(name)
        discord.InteractionResponse._perf_patched = True

    tree = bot.tree
    prior_check = tree.interaction_check

    async def interaction_check(interaction: discord.Interaction) -> bool:
        try:
            interaction.extras["t0"] = time.perf_counter()
            if interaction.created_at is not None:
                import datetime as _dt

                delay = (_dt.datetime.now(_dt.timezone.utc) - interaction.created_at).total_seconds() * 1000
                if 0 <= delay < 60000:
                    record("gateway_delay", delay)
        except Exception:
            pass
        return await prior_check(interaction)

    tree.interaction_check = interaction_check

    def _finish(interaction, command, ok: bool):
        try:
            if interaction is None:
                return
            t0 = getattr(interaction, "extras", {}).get("t0")
            if t0 is None:
                return
            name = getattr(command, "qualified_name", None) or getattr(command, "name", "unknown")
            total = (time.perf_counter() - t0) * 1000
            record(f"cmd.{name}.total", total)
            t_ack = interaction.extras.get("t_ack")
            if t_ack is not None:
                ack = (t_ack - t0) * 1000
                record(f"cmd.{name}.ack", ack)
                if ack > SLOW_MS["ack"]:
                    log.warning("slow_ack cmd=%s ms=%.0f", name, ack)
            if not ok:
                count(f"cmd.{name}.errors")
            if total > SLOW_MS["total"]:
                log.warning("slow_command cmd=%s total_ms=%.0f", name, total)
        except Exception:
            log.debug("perf finish failed", exc_info=True)

    # Listeners in discord.py must be coroutines (async def)
    async def on_app_command_completion(interaction: discord.Interaction, command) -> None:
        _finish(interaction, command, True)

    bot.add_listener(on_app_command_completion, "on_app_command_completion")

    prior_tree_error = tree.on_error

    async def on_tree_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
        try:
            cmd = getattr(interaction, "command", None)
            _finish(interaction, cmd, False)
        except Exception:
            pass
        return await prior_tree_error(interaction, error)

    tree.on_error = on_tree_error
