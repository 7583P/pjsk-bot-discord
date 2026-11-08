# Rotación de credenciales (valores nunca en este documento)

| Credencial | Dónde estuvo expuesta | Dónde se usa | Cómo rotar | Efecto |
|---|---|---|---|---|
| Discord Bot Token | `scripts/test_gateway_live.py` (disco), `.env` local (hash marcado como expuesto) | Railway `DISCORD_TOKEN` → luego Wispbyte | Portal Discord → Bot → Reset Token (acción humana). Cargar el nuevo valor SOLO en Wispbyte antes del arranque | Invalida el token viejo al instante: Railway se desconecta. Hacerlo en el corte, justo antes de iniciar Wispbyte |
| Vercel protection-bypass (2 tokens) | `test_vercel_preview_direct.py`, `test_oauth_session_live.py` (disco; en el historial local sin publicar) | Pruebas de preview | Vercel → proyecto → Settings → Deployment Protection → regenerar/revocar | Sin impacto en producción |
| `SESSION_SECRET` | `test_oauth_session_live.py` lo leía de Railway | API (firma de cookies de sesión) | Nuevo valor en el host de la API en el corte | Cierra todas las sesiones web: los usuarios vuelven a iniciar sesión con Discord |
| `DISCORD_CLIENT_SECRET` | copiado a variables Preview de Vercel (sensible) | OAuth | Opcional: Reset Secret en el portal tras el corte | Actualizar Railway/Vercel a la vez |
| Contraseña de Supabase `postgres` | usada en Preview de Vercel y Railway (sensible) | bot + API | Supabase → Database → Reset password; actualizar Railway, Wispbyte, Vercel | Cortar conexiones activas: hacerlo en el corte |

Orden sugerido en el corte: (1) sin salas activas, (2) Railway bot detenido, (3) rotar token Discord, (4) cargar token en Wispbyte, (5) arrancar `python run_bot.py`, (6) rotar `SESSION_SECRET` al cambiar rewrites de la API.
Los archivos con credenciales están ignorados por Git y excluidos de las ramas publicadas; bórralos del disco tras rotar.
