# Corte Railway → Wispbyte (bot) + Vercel Functions (API) — runbook

Estado base: Season 5 activa · fingerprint MMR `9d3e14332158b8f39d7b9c8e73d3f219` (cambia legítimamente con partidas).
**Nunca** detener Railway con salas activas: `python -m scripts.cutover_preflight --from-railway` debe dar **GO**.

## ⚠️ Regla Railway (aprendida el 2026-11-08)
El servicio `worker` está conectado a GitHub `main` (8a4f812, código antiguo), pero producción corre un despliegue **subido por CLI** (id `bccf32a0`, 4-nov). **Cualquier cambio de variable dispara un build desde GitHub `main`** y reemplaza el código de producción por el antiguo (sin lease, con pools propios → `EMAXCONNSESSION`).
- Para cambiar variables sin cambiar código: `variableCollectionUpsert` con `skipDeploys:true` y luego `deploymentRollback(bccf32a0-cdce-489b-ac64-247b469cb76c)` — ver `scripts/railway_restore_upload_deploy.py`.
- Parar el bot en el corte (paso 2) = `DISABLED<token>` (dispara build de main, solo API) → esperar lease libre → restaurar token con `skipDeploys` → rollback. No usar `railway variables --set` suelto.
- Rollback de emergencia del bot: ese mismo script restaura el despliegue del 4-nov.

## Orden (cada paso tiene su rollback)
| # | Acción | Rollback |
|---|---|---|
| 0 | Preflight GO (sin salas activas/colas/publicaciones pendientes). Anunciar ventana | — |
| 1 | Wispbyte validado con `probe.py` (RTT/CPU/RAM aceptables vs Railway) y Preview OAuth OK | — |
| 2 | **Parar el bot en Railway manteniendo la API**: variable `DISCORD_TOKEN=DISABLED` (run.py ya lo soporta: el bot no conecta, FastAPI sigue). Esperar a que `lounge_runtime_lease` expire (≤45 s) y repetir preflight | Restaurar el valor original de `DISCORD_TOKEN` |
| 3 | Rotar el token en el portal de Discord (invalida el viejo). Cargarlo **solo** en Wispbyte (`DISCORD_TOKEN`) | Poner el token nuevo en Railway y quitar `DISABLED`; parar Wispbyte |
| 4 | Wispbyte: variables de `docs/WISPBYTE.md`; Startup `python run_bot.py`; Start. Vigilar logs: `Cog cargado` ×5, `Slash commands sincronizados` (incluye `reroll`), `/health/ready` | Parar Wispbyte → pasos 3-rollback |
| 5 | Pruebas Gateway real (ver abajo) y `scripts.post_reroll_verify` | — |
| 6 | Registrar 008 **solo después del paso 8** (la API vieja de Railway exige versión == 007 y dejaría de arrancar): `python -m scripts.register_schema_008 --from-railway --confirm-no-007-runtime` | `DELETE FROM lounge_schema_migrations WHERE version='008_room_reroll.sql'` |
| 7 | API → Vercel Functions (abajo) | Rollback instantáneo de Vercel a la despliegue anterior |
| 8 | Railway queda como fallback ≥7 días (API intacta, bot en DISABLED). Luego retirar | — |

## API en Vercel (producción)  — no ejecutado
1. `python -m scripts.enable_lounge_api_login --bundle <bundle> --targets preview,production` (rota la contraseña de `lounge_api` y la guarda en ambos entornos; sensible).
2. `python -m scripts.vercel_preview_env <bundle> pjsk-page.vercel.app production --execute` (SESSION_SECRET nuevo, OAuth, FRONT/CORS, redirect `https://pjsk-page.vercel.app/auth02/callback` — ya registrada en Discord).
3. `vercel deploy --prod` desde `artifacts/vercel-preview-*` reconstruido (`scripts.build_vercel_preview`). El bundle ya trae estáticos + FastAPI y **no tiene rewrites a Railway**: ese despliegue es el cambio de rewrites.
4. Verificar con tu navegador: login real, `/auth02/me`, títulos, logout. Si falla: `vercel rollback` (vuelve al despliegue previo con rewrites a Railway; Railway sigue sirviendo la API).

## Rotación sin tocar partidas
- **Discord Bot Token**: paso 3 (solo cuando no hay salas). El bot en Wispbyte reconecta en <1 min; las salas viven en BD.
- **SESSION_SECRET**: nuevo valor solo en Vercel prod en el paso 7. Cierra las sesiones web (relogin con Discord); el bot no lo usa. Railway conserva el viejo mientras sea fallback.
- **OAuth Client Secret**: reset en el portal → actualizar de inmediato Vercel prod (y Railway si aún sirve la API) → redeploy. Login falla unos minutos: hacerlo fuera de hora punta.
- **Contraseña `postgres` de Supabase**: lo último; actualizar Railway, Wispbyte (`DATABASE_URL`) y `scripts` locales; Vercel usa `lounge_api` y no se ve afectado.
- **Bypass de Vercel**: revocar en Settings → Deployment Protection.

## Pruebas de /reroll en Gateway real (tras el paso 4)
Candidata real: la sala 1 (`STARTED`, 5 jugadores, `songs_posted=False`) detectada por el preflight — puede seguir así hasta el corte.
1. Con otra cuenta (no 878310498720940102, aunque sea admin): `/reroll` → respuesta privada "not allowed". 
2. Con la cuenta autorizada, dentro del hilo de la sala: `/reroll reason:"selección fallida"` → mensaje fijado "Song selection recovered", mismas personas, sin `/c` ni `/d`.
3. `python -m scripts.post_reroll_verify --from-railway --thread-id <id> --baseline <fingerprint>` → auditoría `published`, `songs_posted=True`, MMR igual.
4. Parar y arrancar Wispbyte; la sala sigue en `lounge_room_state` y el bot la restaura; ningún mensaje duplicado.
5. Dos `/reroll` casi simultáneos: uno responde "another operation…" o se serializan; `reroll_count` coincide con auditorías.
6. Enviar el resultado normal con `!submit` solo en una sala real; `/reroll` posterior debe responder "definitive result".


---
## Estado medido (2026-11-08) y proyección honesta
| Dato | Valor | Fuente |
|---|---|---|
| RTT a Supabase desde Wispbyte (Rumanía) | **244 ms** (`SELECT 1` p50; consulta tipo top100 p50 244, p95 488) | probe real en Wispbyte |
| RTT desde Railway | **≈140–215 ms** (inferido: el chequeo de BD de `/health` añade ≈432 ms con 2–3 viajes) | medición indirecta, sin acceso al contenedor |
| Render `/top100` por página | Wispbyte **1920 ms** · PC 90–140 ms | probe real |
| RAM pico | 94 MB de 512 | probe real |
| Discord REST | 81 ms p50 desde Wispbyte | probe real |

**Conclusión:** cambiar a Wispbyte **no mejora la latencia de BD** (probablemente la empeora ≈1.1–1.7×) y su CPU es ~15–20× más lenta que mi PC. La mejora real viene del código (menos viajes por comando, caché de temporadas, render cacheado) y de mover la **API** a Vercel `gru1` (misma región que Supabase). Proyección por componentes (NO medida): `/mmr` ≈ 0.5–1.0 s; `/top100` ≈ 3–3.5 s en frío y ≈ 1.2 s con caché caliente (precalentada cada 10 min y persistente en disco); `/c`,`/d` ≈ 1–2 s. Se confirmarán con `/health/metrics` en producción (p50/p95 reales).
Alternativa si Wispbyte no cumple: cualquier host en `sa-east`/`us-east` (RTT <40 ms) con ≥1 vCPU; el paquete es agnóstico (`python -u bootstrap.py run_bot.py`).

## Credenciales del bot definitivo (pendiente de tu autorización)
`scripts/create_bot_login.py` habilita login en `lounge_bot` (no cambia permisos; ya cubren las 18 tablas que escribe el bot). Hasta autorizarlo el bot usaría `postgres` (no recomendado). Rollback: `--disable`.

## Corte con Railway como fallback (API en Vercel ya verificada)
1. Preflight GO · 2. `python -m scripts.railway_restore_upload_deploy bccf32a0-cdce-489b-ac64-247b469cb76c --keep-disabled` (Gateway de Railway OFF, API del build subido ON, sin rebuild de `main`) · 3. rotar token en el portal · 4. Wispbyte: `.env` con DSN del bot + `DISCORD_TOKEN`, Startup `python -u bootstrap.py run_bot.py` · 5. verificar `lounge_runtime_lease`, comandos, `/health/metrics`.
**Rollback:** parar Wispbyte → `python -m scripts.railway_restore_upload_deploy bccf32a0-cdce-489b-ac64-247b469cb76c` (sin flag: restaura el token y relanza el bot en Railway).
