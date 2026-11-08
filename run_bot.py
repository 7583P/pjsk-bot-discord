"""Supervisor for the Discord bot on a panel host (Wispbyte): restart with backoff.

Runs `python main.py` as a child so every restart gets a fresh Gateway client
(discord.Client cannot be reused once closed). Backoff avoids reconnect storms
that could trip Discord's rate limits on a shared IP; a live DB lease held by
another instance (e.g. Railway still running) is never fought over.
"""
import logging, os, signal, subprocess, sys, time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s run_bot: %(message)s")
log = logging.getLogger("run_bot")
child = None
stopping = False

def _stop(*_):
    global stopping
    stopping = True
    if child and child.poll() is None:
        child.terminate()

signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

delay, MIN, MAX = 15, 15, 300
while not stopping:
    started = time.monotonic()
    log.info("starting bot process")
    child = subprocess.Popen([sys.executable, "main.py"])
    code = child.wait()
    ran = time.monotonic() - started
    if stopping:
        break
    # A healthy long run resets the backoff; quick crashes back off exponentially.
    delay = MIN if ran > 600 else min(MAX, delay * 2)
    log.warning("bot exited code=%s after %.0fs; restart in %ss", code, ran, delay)
    for _ in range(int(delay)):
        if stopping:
            break
        time.sleep(1)
sys.exit(0)
