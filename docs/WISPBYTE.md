# Despliegue del bot en Wispbyte (solo bot)

Paquete: `python -m scripts.build_wispbyte_package` → `artifacts/wispbyte-package-<ts>/` (+ `.zip`), sin secretos, `.env`, DB ni cachés.

- **Startup command:** `python run_bot.py` (supervisor con backoff; lanza `python main.py`). Python ≥ 3.11 (probado 3.13).
- **Dependencias:** `pip install -r requirements-bot.txt` (sin FastAPI/uvicorn).
- **Variables (panel Startup/Environment, nunca en archivos):**
  `DISCORD_TOKEN`, `APPLICATION_ID=1371546746248761455`, `GUILD_ID=1371306026917298267`,
  `DATABASE_URL` (pooler de Supabase en modo *session*, puerto 5432), `DB_SSL=true`,
  `DB_CA_FILE=certs/supabase_ca_bundle.crt`, `DB_POOL_MODE=session`, `DB_POOL_MAX=3`,
  `LOG_CHANNEL_ID` (opcional). No definir `PORT` ni exponer el puerto 8081 (diagnóstico solo loopback).
- **Antes de arrancar en producción:** `python probe.py` en la consola de Wispbyte (RTT a Supabase, alcance de Discord, CPU).
- **Exclusividad:** la lease en BD impide dos escritores nuevos, pero NO bloquea el bot viejo: Railway debe estar detenido (y sin salas activas) antes del arranque.

## Arquitectura
Bot → Wispbyte. DB → Supabase. Web → Vercel. **API FastAPI → sigue en Railway**: `web/vercel.json` reenvía `/auth02/*` y `/api/*` a `worker-production-1ad2.up.railway.app`. Para retirar Railway hace falta antes un host HTTPS alternativo para la API (Vercel serverless con el paquete `artifacts/vercel-*` o similar) y cambiar esos rewrites. Cuando el bot salga de Railway, su start command pasa a `python -m uvicorn app:app --host 0.0.0.0 --port $PORT` (solo API).

## Rollback
Detener Wispbyte → restaurar el start command `python run.py` en Railway (el esquema sigue en 007; la tabla 008 es aditiva y ignorada por el código viejo).
