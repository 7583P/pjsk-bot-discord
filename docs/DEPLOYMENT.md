# Operación y despliegue

> Documento histórico del prototipo basado en `090e97f`, conservado como trabajo previo. No ejecutar sus importadores/reset sobre producción. El backend moderno se recuperó en `8a4f812`; utilizar [CURRENT_DEPLOYMENT.md](CURRENT_DEPLOYMENT.md) y [REPORT.md](REPORT.md). Las menciones siguientes a backend ausente, rangos, OAuth y roles de solo lectura ya no describen el candidato vigente.

Este documento prepara el despliegue; no certifica que haya ocurrido. No se crearon recursos externos ni se activaron planes pagos.

## Precondiciones

1. Rotar el token en Discord Developer Portal → aplicación existente → Bot → Reset Token. Guardarlo solo en el entorno del bot. Tres versiones históricas de `.env` contienen el token configurado actualmente. Los hashes históricos quedan bloqueados en `lounge/exposed_token_hashes.json`, sin texto del secreto. No se reescribió el repositorio original; limpiar el historial requiere un procedimiento coordinado adicional.
2. Confirmar que este checkout es la versión competitiva que se quiere desplegar. No contiene Iron–Legend, pairwise, perfiles con OAuth ni galerías. Si existe otro repositorio, auditarlo y caracterizar sus reglas antes de sustituirlo.
3. Acceso autorizado a Supabase Free, Vercel Hobby compatible con sus condiciones y una VM Always Free disponible. Copiar IDs existentes; no reemplazarlos por ejemplos.
4. Detener los antiguos escritores Python/SQLite/Node/Heroku y servicios de notificación. El código Node anterior ahora termina con instrucciones; no inicia otro Gateway.
5. Habilitar Server Members y Message Content en Discord. Permisos: View Channel, Send Messages, Create Public Threads, Send Messages in Threads, Read Message History; Manage Threads para cerrar/renombrar y Manage Roles para rangos. El rol del bot debe estar encima de los rangos existentes. No se crean ni cambian sus colores.

## Entorno y conexiones

Python 3.13, `python -m pip install -r requirements.lock`. El lock incluye dependencias transitivas verificadas. Para QA instalar requirements-dev.txt. Instalar servidor PostgreSQL local para restauraciones y clientes pg_dump/pg_restore de versión compatible con el origen.

Variables en `.env.example`. `DATABASE_URL` es privada del bot; para migración/importación/backup/activación se utiliza temporalmente una conexión administrativa autorizada, después se cambia por lounge_bot. `API_DATABASE_URL` es un login distinto de lectura; no configurar el token Discord en Vercel. Copiar la URI del panel; codificar correctamente caracteres especiales en contraseñas.

Bot: DB_POOL_MODE=session para Supavisor IPv4 o direct para IPv6, DB_POOL_MAX=4. API: DB_POOL_MODE=transaction y DB_POOL_MAX=1, con statement cache desactivada. DB_SSL=true; DB_CA_FILE opcional apunta a la CA descargada del panel cuando no esté en el trust store. Un valor vacío se interpreta como no configurado. Local: DB_SSL=false solo se admite con loopback. No desactivar TLS para Supabase.

`QUEUES_FILE` apunta a un array JSON con guild_id y channel_id como strings. Incluir cada cola real y comprobar sus permisos. Sin archivo se conserva GUILD_ID/canal `join`, separado por ID real de canal. No se encontraron IDs JP-PJSK/PJSK-QUEUE en este checkout.

## Backup e importación inicial

Con los escritores antiguos detenidos, desde la raíz:

```sh
python -m scripts.backup_sqlite matchmaking.db backups/legacy-final.db
python -m scripts.migrate
python -m scripts.import_legacy backups/legacy-final.db --active-label beta
```

Usar beta solo si la auditoría de la fuente definitiva lo confirma. El backup usa SQLite online, integrity_check, restauración aislada y comparación de esquema/recuentos; genera manifest SHA-256. No copia el archivo ignorando el journal, ni borra/modifica el original.

La importación requiere destino vacío y manifest verificado. Copia IDs bigint, nombres, países, MMR, rol, temporada y JSON íntegro de la fila original; preserva placements aunque estuviera inactiva. Reimportar el mismo snapshot es un no-op; otro snapshot sobre datos existentes se rechaza. No reconstruye historiales que nunca se guardaron. El ID de mmr_data.json ya está en SQLite con igual MMR=0; ranks.json está vacío.

Aplicar `deploy/roles.sql` como propietario de migraciones, por editor SQL o psql con credenciales en el entorno. No contiene passwords. Habilitar LOGIN para lounge_bot y lounge_api y asignar contraseñas en privado; psql admite `\password lounge_api`. Las políticas RLS permiten sus accesos concretos; anon/authenticated carecen de acceso directo. lounge_api no puede leer legacy_data ni modificar season_players. No usar el administrador en runtime.

## Ensayo aislado

Configurar TEST_DATABASE_ADMIN_URL con servidor de loopback y PG_BIN_DIR con clientes compatibles:

```sh
python -m scripts.staging_rehearsal backups/legacy-final.db --active-label beta
```

Crea bases aleatorias pjsk_test_rehearsal_* y pjsk_restore_*, migra/importa dos veces, mide consultas/API, restaura pg_dump, activa Season 5 allí y compara datos. Borra únicamente esas bases al finalizar. Genera docs/EVIDENCE.json sin nombres ni IDs. No modifica SQLite ni conecta a Discord.

## Activación productiva

La ejecución real está solicitada, pero requiere fuente definitiva y accesos. Detener el bot y resolver todas las salas abiertas/needs_attention; no finalizar partidas arbitrariamente para pasar el preflight.

Crear una base local **vacía** pjsk_restore_... y configurar RESTORE_DATABASE_URL para ella. DATABASE_URL administrativa apunta al origen Supabase:

```sh
python -m scripts.activate_season_5 --dry-run
python -m scripts.backup_postgres backups/pre-season5.dump --bin-dir /ruta/a/postgresql/bin
python -m scripts.activate_season_5 --execute --backup-manifest backups/pre-season5.manifest.json
python check_reset.py
```

El backup incluye esquema público/datos/políticas; excluye esquemas administrados Supabase y passwords. Restaura localmente, crea nombres de roles NOLOGIN requeridos por políticas y compara SHA-256 de filas de cada tabla. Rechaza un destino con tablas. La activación verifica hash, restauración, antigüedad <24 h y huella del estado actual. Si los datos cambiaron, crear otro backup.

La activación bloquea la fila de temporada, rechaza lease activa/salas pendientes/Season 5 existente y archiva los standings anteriores en la misma transacción que crea los nuevos a cero. Rol inicial Placement; placement_pending=false para todos los jugadores de Season 5, incluidos nuevos y retornados. Peak=0 y partidas/victorias/derrotas=0. Registra UTC real; la web muestra America/Lima. No inventa fecha final. Una sala antigua no puede actualizar la nueva temporada.

Después cambiar DATABASE_URL al usuario bot y arrancar una sola instancia. Verificar API, MMR y roles. La activación no se llama desde main.py ni CI; un ID único y Season 5 existente impiden repetirla.

## VM y supervisión

En Linux crear usuario lounge, preparar /opt/pjsk-lounge con venv/lock y guardar configuración privada en /etc/pjsk-lounge.env con permisos restringidos. Instalar deploy/pjsk-lounge.service:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now pjsk-lounge
sudo systemctl status pjsk-lounge
journalctl -u pjsk-lounge -n 100 --no-pager
curl --fail http://127.0.0.1:8081/health/live
curl --fail http://127.0.0.1:8081/health/ready
curl --fail http://127.0.0.1:8081/health/diagnostics
```

Los endpoints del bot solo escuchan en loopback. live comprueba proceso; ready separa Gateway/DB; diagnostics devuelve temporada, salas/polls, trabajos y pool, sin secretos. Reinicio tras fallo: 20 s, máximo 5 intentos/300 s. SIGTERM cierra cogs/Gateway/lease/pool; perder la lease termina con error para permitir reinicio supervisado. Cada escritura competitiva comprueba owner y caducidad; una segunda instancia no puede reclamar una lease vigente.

Contenedor alternativo: `docker build -t pjsk-lounge .` y `docker run --name pjsk-lounge --restart=on-failure:5 --env-file /ruta/privada/bot.env pjsk-lounge`. No combinarlo con otra instancia systemd. Docker no está instalado aquí: build y ejecución ARM/x86 siguen pendientes. No hay keep-alive externo ni tráfico artificial.

## Vercel

Framework FastAPI, Python 3.13 y entrypoint app:app declarado en pyproject.toml. public/ se sirve por CDN en Vercel y StaticFiles solo en local. Configurar API_DATABASE_URL, DB_POOL_MODE=transaction, DB_POOL_MAX=1, DB_SSL=true y CA si corresponde. No incluir el token Discord. .vercelignore excluye secretos, bases, dumps, Git, cogs y herramientas locales.

```sh
npm run build
vercel deploy
# Revisar preview con staging; después promover deliberadamente:
vercel deploy --prod
```

Los comandos Vercel no se ejecutaron: falta acceso/proyecto. Revisar /, /app.js, /api/health, /api/season, /api/players y búsqueda/paginación. No hay OAuth ni cuentas de perfil en el código recibido; no se implementó un formulario inseguro basado en discordId. No se encontró configuración de dominio existente.

## Conciliación y mantenimiento

Se restauran salas y views con el mismo custom_id/deadline; una votación vencida se cierra sin recibir otros 60 s. Una entrega ambigua de thread/poll queda needs_attention y no se recrea a ciegas. Un operador debe verificar Discord y asociar el ID real o resolver ese estado explícitamente. No se inventa una política de resultados para partidas incompletas.

role_jobs y thread_jobs persisten intentos/fallos/próxima ejecución con backoff limitado y jitter. Solo modifican rangos competitivos o membresías. Un thread archivado cierra la sala, libera miembros y cancela el poll; no se reabre para evadir caducidad. Un resultado confirmado no se revierte por un fallo de roles. Un submit repetido devuelve el anterior; cada sala tiene una partida confirmable y finaliza al registrarla. Para otra partida se usa otra sala.

Logs JSON: Gateway, reconexiones, comandos, transacciones y fallos de conciliación. Errores incluyen tipo/SQLSTATE/frames sin DSN/passwords. Acknowledgement antes de DB/REST. Catálogo en memoria/PostgreSQL cada 6 h con timeout/validación; un fallo conserva el último válido.

Mantener backups fuera de Git/builds y una copia cifrada fuera de la VM. Programar ventana semanal de backup/restauración mientras almacenamiento/transferencia gratuitos lo permitan; vigilar crecimiento antes de fijar retención. No asumir backups administrados en Supabase Free.

## Rollback

Código: volver a una versión compatible con PostgreSQL y el esquema vigente; no reiniciar el escritor SQLite. Datos: detener bot, guardar estado actual, restaurar dump en una base nueva autorizada, aplicar roles/credenciales y validar antes de cambiar URLs bot/API. Reconciliar partidas posteriores al backup; no descartarlas implícitamente. Nunca ensayar DROP/CLEAN sobre producción. La restauración aislada fue verificada; el rollback en infraestructura real sigue pendiente.
