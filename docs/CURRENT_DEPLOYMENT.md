# Operación del candidato moderno

Este procedimiento reemplaza las instrucciones de lanzamiento del prototipo 2025. Trabajar en `.local/season5-current/`, rama `dev/season5-current`. La raíz conserva trabajo previo; no ejecutar sus importadores SQLite, reset o backend sobre la población definitiva.

## Verificación local

Python 3.13 y clientes PostgreSQL compatibles con el servidor de origen. Las variables contienen secretos y se configuran en el entorno privado; `.env.example` solo describe nombres.

```powershell
# Desde D:\Projects\PJSK Bot\.local\season5-current
& '..\..\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt
$env:TEST_DATABASE_ADMIN_URL='postgresql://postgres@127.0.0.1:55432/postgres'
$env:PG_BIN_DIR=(Resolve-Path '../postgres/bin-dist/bin').Path
& '..\..\.venv\Scripts\python.exe' -m scripts.test_suite
```

Este URL de ejemplo es exclusivamente el servidor local desechable instalado en esta sesión, sin credenciales productivas. La suite crea/elimina únicamente bases locales de prueba. Una omisión hace fallar el verificador completo. La evidencia queda en `artifacts/current-tests.json` del worktree.

En `web/`: `npm.cmd ci`, `npm.cmd test`, `npm.cmd run build`. Los tests usan DOM simulado; no sustituyen QA de navegador. El build genera assets Vue/Axios con hash a partir del lock.

## Inspección y staging

1. Obtener el dump y esquema del backend moderno definitivo. Revisar fuente de escrituras, tablas, tipos, índices, duplicados, temporadas, roles, títulos, avatares y archivos. No usar la SQLite histórica como sustituto.
2. Restaurar en una base aislada. `python -m scripts.inspect_source` consulta metadata/recuentos/temporadas sin imprimir jugadores. Usar una conexión administrativa autorizada a esa copia en `API_DATABASE_URL`; el nombre de variable no determina los permisos del login.
3. `python -m scripts.migrate_runtime` no modifica nada. `python -m scripts.migrate_runtime --apply` aplica las migraciones verificadas `001`–`007`; requiere privilegios para DDL/roles. No se ejecuta al arrancar ni importa fixtures de prueba.
4. Crear logins privados para los roles sin conceder BYPASSRLS ni ownership; verificar `lounge_api` sin permisos MMR. El bot usa `DATABASE_URL`; la API `API_DATABASE_URL`. Conservar URLs/tipos/IDs originales y verificar snapshot restaurado.
5. Usar pool session/direct para el bot (máximo 4), transaction para API (máximo 1, sin prepared statement cache). `DB_SSL=true`; instalar CA verificable si hace falta. `DB_POOL_MODE` y `DB_POOL_MAX` se configuran por servicio. Copiar los endpoints reales del panel Supabase.

## Procesos y autenticación

Bot: `python main.py`. API local separada: `python -m uvicorn app:app --host 127.0.0.1 --port 8000`. No usar `run.py` para producción ni levantar el Gateway en Vercel. `Procfile` moderno ejecuta solamente `main.py`. No correr systemd y Docker a la vez. El Dockerfile y `deploy/pjsk-lounge.service` modernos son candidatos pendientes de prueba Linux; no utilizar los del prototipo antiguo por accidente.

Configurar los IDs existentes de aplicación/guild y preservar los IDs EN/JP/roles del código moderno. Activar los intents Members/Message Content y comprobar permisos para threads, mensajes y roles. Rotar el token histórico. La lease solo evita duplicados de la versión nueva: **no bloquea al antiguo escritor**, que debe detenerse explícitamente.

API: configurar `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, `FRONTEND_URL`, `SESSION_SECRET` y DB. No configurar `DISCORD_TOKEN` en Vercel. El redirect debe volver al mismo dominio HTTPS público por `/auth02/callback`, registrado exactamente en Discord. Conservar la clave de sesión anterior durante la transición solo si es segura y conocida; una rotación invalida sesiones anteriores. Probar cookie-only antes de activar `AUTH_COOKIE_ONLY=true`. El refresco continuo de cartas no se ejecuta dentro de Vercel; sus endpoints siguen disponibles y el catálogo requiere una tarea administrativa explícita si se utiliza.

`GET /health` conserva el contrato histórico con la señal `db`; un HTTP 200 por sí solo no prueba que DB esté disponible. API `/health/ready` responde 503 si DB falla. El bot sirve `/health/live`, `/health/ready` y `/health/diagnostics` exclusivamente en 127.0.0.1:8081 (BOT_HEALTH_PORT configurable). Readiness exige Gateway, DB, lease y recuperación. Supervisión real prolongada y alertas externas siguen pendientes.

## Paquete Vercel local

Desde el worktree moderno, después del build web:

```powershell
& '..\..\.venv\Scripts\python.exe' -m scripts.package_vercel --web ../../web --destination ../../artifacts/OTRO-CANDIDATO-NUEVO
```

El destino debe ser nuevo; el empaquetador no sobrescribe archivos. Copia únicamente API, librería, lock y web/assets; no incluye `.env`, cogs, Git ni dumps. Rechaza assets privados y symlinks, normaliza EOL de fuentes y genera manifiesto SHA-256. El paquete final está en `artifacts/vercel-production-prep-final-20261101/` de la raíz; se conservaron los anteriores. No se ejecutó el build remoto Vercel. Configurar primero staging conforme a [SUPABASE_SETUP.md](SUPABASE_SETUP.md) y [QA_GATEWAY_PREVIEW.md](QA_GATEWAY_PREVIEW.md); no cambiar los rewrites productivos antes del corte.

## Conciliación antes del cambio estacional

Detener nuevas entradas y drenar los jugadores. Resolver salas `active`/`needs_attention` y colas persistidas. Un voto interrumpido no vuelve a empezar automáticamente. Verificar en Discord el thread y sus mensajes antes de cualquier corrección administrativa; registrar operador y motivo. No cambiar payloads ni temporadas a ciegas.

Los resultados y su publicación son operaciones distintas. Consultar `lounge_submissions WHERE publication_status='pending'`, correlacionar `result_id` con `results` y el thread. Si Discord recibió el mensaje antes de fallar el acknowledgement, asociar el mensaje real en una transacción auditada; si no lo recibió, publicar mediante una herramienta operativa revisada. No invocar nuevamente el cálculo MMR ni marcar todo como publicado. Una herramienta de reenvío automático está pendiente; no se afirma entrega exactamente una vez en Discord.

`lounge_role_jobs` conserva reintentos con backoff y obtiene el rango vigente de la DB. Verificar jerarquía/permisos del bot y el vaciado de trabajos antes del corte.

## Backup y ensayo Season 5

Con el bot detenido, configurar `API_DATABASE_URL` con permiso administrativo y `RESTORE_DATABASE_URL` hacia una base **local vacía** de restauración `pjsk_restore_*`. No utilizar un destino con datos. La herramienta rechaza otro destino.

```text
python -m scripts.backup_postgres backups/before.dump --bin-dir RUTA_CLIENTES_POSTGRES
python -m scripts.activate_season_5
```

El backup usa `pg_dump` custom/public, restaura de verdad y compara hashes de todas las tablas públicas; credenciales van por entorno de subprocess, no argumentos. Proyectos con datos relevantes fuera de `public` requieren además el backup de esos esquemas, almacenamiento y configuración antes de considerarlo completo. Guardar dump/manifiesto fuera de Git, con acceso limitado y una copia externa.

`activate_season_5` es dry-run salvo `--execute`. **Su ejecución está deshabilitada fuera de localhost/pjsk_test_***. El ensayo requiere respaldo restaurado menor de 24 h, estado idéntico al respaldado, bot detenido y pendientes resueltos. Mantener este bloqueo: no existe autorización de reset productivo en esta entrega. Revisar `--help` para argumentos de manifiesto del ensayo, sin copiar comandos del prototipo antiguo.

## Cutover y rollback pendientes

1. Validar staging/API/web/Discord, respaldos y capacidad; anotar hashes revisados y propietarios de credenciales. No habilitar el nuevo bot en la base definitiva mientras siga escribiendo el servicio antiguo.
2. Ventana acordada: drenar, parar y deshabilitar reinicios de todos los escritores anteriores, verificar ausencia de actividad, exportar y restaurar backup final. Conservar configuración y respaldos del servicio anterior.
3. Restaurar/importar fuente definitiva en Supabase, aplicar migraciones revisadas y verificar integridad/RLS. Si hay datos nuevos en el origen desde el snapshot, repetir el corte; no combinar filas por intuición.
4. Levantar una única instancia nueva y preview API contra la misma fuente; comprobar lecturas/escrituras autorizadas, roles y recuperación. Promover dominio/rutas HTTPS solo después de resultados satisfactorios. Season 5 requiere una operación separada autorizada; no se activa por publicar el frontend.
5. Si falla antes de escrituras nuevas: parar el nuevo escritor y restaurar rutas/configuración antigua verificadas. Si ya hubo resultados: detener ambos, conservar estado nuevo y reconciliar el delta antes de restaurar una copia; no descartar partidas posteriores al backup ni arrancar ambos servicios.

Ningún paso productivo anterior se ejecutó en esta sesión.
