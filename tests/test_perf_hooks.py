import asyncio
import inspect
import time
import discord
from discord.ext import commands
from lounge import perf

def test_hooks():
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)
    
    # 1. Install hooks
    perf.install_discord_hooks(bot)
    
    # Verify listener is registered and is a coroutine
    listeners = bot.extra_events.get("on_app_command_completion", [])
    print("on_app_command_completion listeners:", listeners)
    assert len(listeners) == 1, "Expected exactly 1 listener"
    listener = listeners[0]
    assert asyncio.iscoroutinefunction(listener), "Listener must be a coroutine function!"
    
    # 2. Test idempotency: calling install_discord_hooks again should not duplicate listener
    perf.install_discord_hooks(bot)
    listeners2 = bot.extra_events.get("on_app_command_completion", [])
    assert len(listeners2) == 1, f"Expected 1 listener after re-install, got {len(listeners2)}"
    
    # 3. Simulate calling listener with mock interaction and command
    class MockInteraction:
        extras = {"t0": time.perf_counter() - 0.05, "t_ack": time.perf_counter() - 0.02}
        created_at = None
    
    class MockCommand:
        qualified_name = "test_cmd"
        name = "test_cmd"
        def _has_any_error_handlers(self): return False
    
    async def run_listener_test():
        await listener(MockInteraction(), MockCommand())
        
    asyncio.run(run_listener_test())
    
    snap = perf.snapshot()
    latency = snap["latency"]
    print("Perf snapshot latency after mock command completion:", {k: v for k, v in latency.items() if "test_cmd" in k})
    assert "cmd.test_cmd.total" in latency, "Expected cmd.test_cmd.total in perf snapshot"
    assert "cmd.test_cmd.ack" in latency, "Expected cmd.test_cmd.ack in perf snapshot"
    print("p50:", latency["cmd.test_cmd.total"]["p50_ms"], "p95:", latency["cmd.test_cmd.total"]["p95_ms"])
    
    # 4. Simulate command error through tree.on_error
    class MockErrorInteraction:
        extras = {"t0": time.perf_counter() - 0.1}
        created_at = None
        command = MockCommand()
    
    async def run_error_test():
        await bot.tree.on_error(MockErrorInteraction(), discord.app_commands.AppCommandError("mock failure"))
        
    asyncio.run(run_error_test())
    snap2 = perf.snapshot()
    counters = snap2["counters"]
    print("Perf counters after mock command error:", {k: v for k, v in counters.items() if "test_cmd" in k})
    assert counters.get("cmd.test_cmd.errors") == 1, "Expected cmd.test_cmd.errors count to be 1"
    
    print("ALL TESTS PASSED COMPLETELY!")

if __name__ == "__main__":
    test_hooks()
