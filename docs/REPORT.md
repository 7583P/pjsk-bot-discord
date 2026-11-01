# Informe técnico — backend moderno y Season 5

Fecha: 2026-11-01, actualización de preparación productiva. Estado: **NOT READY para producción**. Candidato probado con la decisión confirmada «0 inicial y Placement original»; la base definitiva, el despliegue vigente y Discord real todavía no están verificados. No hubo push, despliegue ni reset productivo.

## Código y conservación del trabajo

| Ubicación | Rama / base | Uso |
|---|---|---|
| Raíz del workspace | `dev/season5-new-era`, `090e97f` | Trabajo anterior de 2025 conservado; no es el candidato moderno |
| `.local/upstream-bot/` | `main`, `8a4f8127b992562f0a818968e10554256cc88317` | Referencia auténtica recuperada de `7583P/pjsk-bot-discord` |
| `.local/season5-current/` | `dev/season5-current`, base moderna | Implementación y pruebas; worktree con commit independiente |
| `web/` | `dev/season5-web`, base `bc3c069` | Frontend y correcciones, commit independiente |
| `artifacts/vercel-production-prep-final-20261101/` | Copia permitida con manifiesto SHA-256 | API y web combinadas, 28 archivos, sin despliegue |

Se recuperaron los historiales completos accesibles: 658 commits del bot y 431 de la web. Ambos remotes anuncian únicamente `main`; no apareció otra rama remota más reciente. El snapshot previo está en `backups/wip-20261101T111525Z/`. `.local/` y `web/` son repositorios separados ignorados por la raíz: un commit en la raíz **no guarda** sus cambios. No eliminar esas carpetas; revisar/guardar cada rama por separado.

## Backend auténtico y fuente productiva pendiente

El `app.py` de `8a4f812` implementa realmente `/api` y `/auth02`; los cogs modernos implementan Iron–Legend, pairwise, roles y matchmaking EN/JP. El archivo `backend_fastapi_cookie_patch.py` de la web es solo una referencia de cookies. Los rewrites de `web/vercel.json` son proxies, no otro backend.

El destino configurado por la web es `http://78.154.103.25:14646`; el README también contiene una referencia histórica a Railway. Las comprobaciones HTTP públicas realizadas no verificaron un servicio accesible ni su SHA. La procedencia Git está identificada; **no se ha demostrado que ese commit sea exactamente el desplegado**. No hay credenciales autorizadas de la base/hosting disponibles para inspeccionar su estado real. Los 58 jugadores SQLite siguen clasificados como históricos y no se importaron al candidato moderno como población productiva.

## Reglas competitivas preservadas

La referencia es `cogs/matchmaking.py` moderno, con funciones originales congeladas en `tests/fixtures/competitive_8a4f812.py` y contratos en `upstream_contracts.json`, dentro del worktree moderno.

- Pairwise para 2–5 participantes, K=50, escala=400, límite ±39, redondeos y piso 0 originales.
- Scores con pesos PGGBM 4/3/2/1/0; empates y prioridad de Placement originales.
- Rangos cada 50 MMR hasta Legend desde 1000; tabla real de Placement conservada.
- El historial vigente considera victoria un delta bruto >=0. No se reemplazó por «solo primer puesto».
- Season 5 comienza con 0 MMR para todos y **conserva Placement normal y Season Placement**, según la confirmación del usuario. La primera partida mantiene la siembra original (retornantes: anterior−50, piso 0). Se corrigió la interpretación anterior que deshabilitaba Placement; consultar [PLACEMENT_POLICY.md](PLACEMENT_POLICY.md).
- IDs originales de colas/roles y selección EN/JP conservados. Ventanas PREP y slots :00/:30 conservados; no se inventó otro emparejador.
- Los nombres de comandos se conservan. Excepción deliberada: `seasonreset`, `newseason` y `migrate_players_to_newseason` responden que las modificaciones estacionales están pausadas; no permiten saltarse el backup ni el bloqueo productivo.

## Cambios de ingeniería y seguridad

El bot comparte un pool PostgreSQL; las lecturas de MMR por sala usan una consulta por lote. Hay límites de conexiones, TLS verificado para conexiones remotas y statement cache deshabilitada en transaction pooling. Una lease PostgreSQL rechaza instancias duplicadas y cerca la escritura de resultados si caduca.

El submit conserva la votación original y confirma resultado, MMR, historial, canciones, identificador e intentos de roles en una sola transacción. La sala queda vinculada a su temporada original. Reintentos concurrentes devuelven el resultado ya registrado; fallos de constraints revierten toda la operación. No se aceptan salas cerradas, en revisión, de otra temporada ni participantes cambiados.

Salas, participantes y colas se persisten en PostgreSQL. La recuperación pausa el scheduler hasta completarse; un fallo de DB reintenta con espera. Votos/selecciones interrumpidos y threads inaccesibles requieren revisión, sin crear otra votación. La unicidad por guild/usuario impide reservar al mismo jugador en dos salas. Los trabajos de roles se reintentan con backoff. Las publicaciones de resultados quedan `pending` hasta recibir confirmación de Discord. No se reenvían automáticamente mensajes ambiguos: el operador debe comprobar Discord antes de resolverlos. Todavía faltan pruebas reales de reinicio y permisos en un servidor Discord.

En OAuth, el estado se guarda en PostgreSQL, ligado al navegador, con caducidad y consumo único. Las sesiones usan cookies Secure/HttpOnly/SameSite=Lax; sigue disponible Bearer durante la transición. El callback deja de enviar el token en la query. `AUTH_COOKIE_ONLY=true` prepara la retirada del transporte por fragmento y de la devolución de token; debe activarse después de QA con el dominio real. El cliente conserva compatibilidad localStorage durante esta fase. Logout revoca los tokens presentados, no todas las sesiones de la cuenta.

Las escrituras comprueban Origin y sesión en servidor. La identidad se vuelve a resolver desde la vinculación almacenada; los IDs/nombres enviados por el navegador no autorizan otro perfil. La edición de canciones exige admin verificado. Los errores SQL no se envían al cliente. Los IDs Discord se serializan como strings para evitar pérdida de precisión.

En la web se corrigieron XSS de nombre/rango mediante nodos de texto, respuestas tardías de jugadores/partidas, conversión de IDs opacos y activación basada únicamente en un contador. Vue 3.5.44 y Axios 1.21.0 quedan fijados por npm lock y servidos como assets locales. No se rediseñó la página ni se reintrodujo una galería histórica. No se realizó QA visual en navegador.

## Datos y migraciones

La fuente moderna utiliza `seasonplayers`, no el `season_players` de la reconstrucción antigua. Se preservan `players`, `seasons`, `results`, `match_history`, `match_songs`, `songs`, vinculaciones, títulos, avatares, cartas y variantes.

Las migraciones `001`–`007` del worktree moderno incorporan el DDL auténtico de arranque y las tablas operativas. No borran duplicados ni reescriben IDs/tipos. Una inconsistencia que impide crear índices debe resolverse con la fuente real, no mediante eliminación automática. Arrancar bot/API solo valida el esquema; migrar es una operación administrativa explícita, serializada y con checksums.

RLS y roles `lounge_bot`/`lounge_api` separan escrituras competitivas de perfiles. La API puede escribir perfiles y estado OAuth; no puede modificar MMR. No es una credencial de solo lectura. No se conceden accesos a navegador/anon/authenticated. Los logins y contraseñas se configuran fuera de Git.

El fixture `tests/fixtures/current_schema.sql` está inferido del código moderno y es representativo; **no es un dump de producción**. Las migraciones se probaron sobre ese fixture, dos veces para comprobar idempotencia. Deben repetirse sobre un backup restaurado de la fuente definitiva, verificando tipos, índices, duplicados, grants, RLS, archivos y recuentos reales antes de Supabase.

## Season 5

`scripts/activate_season_5.py` usa dry-run por defecto. La ejecución se limita expresamente a bases locales `pjsk_test_*`; producción permanece deshabilitada. Exige bot detenido, colas/salas/publicaciones resueltas, una temporada activa y backup restaurado reciente cuyo hash y huella coincidan con la base. Rechaza repetir una Season 5 existente.

El ensayo archiva la temporada anterior, conserva historiales y perfiles, aplica los títulos de cierre originales, crea exactamente `Season 5 (New Era)`, inicializa MMR/estadísticas en 0 conservando Placement y deja trabajos de reconciliación de roles. El ID nuevo se deriva de los IDs existentes; el número del nombre no impone el ID 5. Se verifica también el primer resultado con siembra original sin alterar el archivo previo. Las fechas visuales no activan datos. El ensayo no equivale a un reset real.

## Verificación ejecutada

Evidencia: [CURRENT_EVIDENCE.json](CURRENT_EVIDENCE.json). La evidencia anterior `EVIDENCE.json` pertenece al esquema de 2025 y no valida este candidato.

| Verificación | Resultado y límite |
|---|---|
| Suite Python moderna | 49 pruebas pasadas, 0 fallos, 0 errores, 0 omisiones; además prueba focalizada del empaquetado normalizado |
| Trabajo previo raíz | 31 pruebas pasadas; validan el prototipo histórico conservado, no las reglas modernas |
| Caracterización MMR | 2000 pairwise, 3001 límites Placement, 1000 cálculos completos, rangos y empates |
| PostgreSQL real local | Migraciones, grants/RLS con roles reales, duplicados concurrentes, rollback, lease y pertenencia |
| API real ASGI + DB | Temporadas, jugadores/estadísticas, partidas, HOF/cutoffs, sesiones/perfiles/títulos; algunas rutas con fixtures pequeños o archivo vacío |
| OAuth Discord | Flujo callback con intercambio HTTP simulado; estado/replay/cookies/revocación con DB real |
| Submit | Votación con reacciones simuladas, cálculo y DB reales; fallo de publicación posterior al commit, sin duplicar MMR |
| Backup/reset | `pg_dump` y `pg_restore` reales, hashes/recuentos, ensayo local, preservación y rechazo del segundo reset |
| Web | 11 pruebas Node/jsdom pasadas: XSS, carreras, IDs, temporada, SP 0, CSS, scripts y proxy HTTPS |
| Build web | Validación pasada; candidato final de 28 archivos, empaquetado reproducible normalizando EOL de fuentes |
| Dependencias | pip check correcto; pip-audit sin vulnerabilidades conocidas en el lock; npm audit producción: 0 hallazgos |
| Sintaxis/estática | compileall; Ruff F/E9 en código nuevo y F821/F822/F823/E9 en API/cogs; diff --check correctos |
| Producción | No verificada: sin Supabase real, Gateway, OAuth real, Vercel remoto ni prueba de carga WAN |

Comandos reproducibles y cutover: [CURRENT_DEPLOYMENT.md](CURRENT_DEPLOYMENT.md). El Dockerfile, systemd y el reconocimiento del candidato por Vercel están preparados, pero no se ejecutaron en sus plataformas; no se afirma que estén desplegados.

## Pendientes que necesitan acceso o decisiones operativas

1. Identificar y exportar con autorización la base que escribe actualmente el backend externo; obtener esquema, archivos, recuentos y configuración sin compartir secretos en el chat ni Git.
2. Rotar el token Discord expuesto en historial y configurar token nuevo, OAuth, SESSION_SECRET y URLs privadas de base. Los hashes de tokens históricos conocidos se rechazan al arrancar.
3. Restaurar el dump definitivo en staging/Supabase, aplicar migraciones y repetir la suite con tipos/datos reales; medir capacidad del plan y conexiones.
4. Validar en Discord de pruebas comandos, EN/JP, roles/permisos, imágenes, votos, cierres, reconexiones y recuperación. Verificar selección de canciones y fallos REST reales; la caracterización numérica no cubre por sí sola todo el Gateway.
5. Validar en preview Vercel rutas/assets, proxy HTTPS, cookies reales, callback, cuentas/vinculaciones, móviles y HOF histórico; aprobar retirada gradual de Bearer/localStorage.
6. Acordar ventana de corte, detener y verificar todos los escritores antiguos, backup final restaurado y rollback. Solo entonces preparar una autorización separada para activar Season 5 en producción; el script actual seguirá rechazándolo.

## Nuevas correcciones y preparación operativa

Antes de esta continuación se respaldaron los tres repositorios en `backups/followup-20261101T124407Z/`: ZIP de fuentes, patch y manifiesto por repositorio (66 archivos modificados/nuevos de raíz, 58 modernos y 9 web en ese momento). Los cambios añadidos después quedan en las ramas y artefactos finales. Se comprueban patrones de secretos sobre los blobs staged antes de cada commit; no se incorporan entornos privados, bases ni dumps.

Se añadieron salud privada por loopback, readiness API con 503 y diagnóstico de build. Staging exige huella revisada de la conexión antes de escrituras y topología Discord separada, completa y explícita. Sin overrides se mantienen los IDs originales. La redacción de logs protege parámetros OAuth, Bearer y URLs privadas sin romper los argumentos del access logger.

La API devuelve estadísticas numéricas en 0 para registros sin partidas. Se probaron cookie-only sin token expuesto, CSRF con cookies e identidad resuelta nuevamente en servidor. Un rol Discord atrasado no puede reseedear un jugador clasificado; la conciliación conserva SP antes de la primera partida. El CSS original se conserva tras normalizar finales de línea.

El inspector verifica huérfanos, parejas estacionales duplicadas, índices, constraints y RLS con consultas de solo lectura. No agrega FK históricas a ciegas ni certifica relaciones sin contrastarlas con el dump real. No se importaron fixtures a Supabase.

Nuevos sondeos: nueve GET públicos anónimos, acotados, sin redirecciones ni datos de jugadores. Vercel: `/health` 404, `/api/runtime-info` 502 y `/api/seasons` sin respuesta válida dentro del límite. Host heredado: las tres consultas fallaron. Referencia Railway: las tres rutas devolvieron 404. Esto no prueba pérdida de datos ni identifica otro escritor. Evidencias agregadas en `artifacts/probe-*-20261101.json` del worktree moderno.

Archivos de esta continuación: `scripts/activate_season_5.py`, `lounge/current_results.py`, `lounge/connections.py`, nuevos módulos `diagnostics`, `health`, `topology`, ajustes de logging, main/API y cogs de matchmaking/roles/players/rooms; inspector, escáner, checker de entorno, sonda pública, empaquetador y pruebas. Web añade configuración HTTPS candidata, pruebas de SP/CSS y normalización Git. En raíz solo se ajustaron documentación, ignore y un placeholder de test; la implementación histórica se conserva.

Acciones concretas del administrador y accesos pendientes:

- [Rotación exacta y variables privadas](SECURITY_ROTATION.md): token nuevo del bot, OAuth, SESSION_SECRET, URLs administrativas/limitadas de DB y dominios. Ninguno se imprimió ni se configuró en cuentas externas.
- [Creación de Supabase, restore, roles y staging](SUPABASE_SETUP.md): proyectos separados, fuente verificada, constraints e integridad reales, logins/Supavisor y huella revisada por servicio.
- [Matriz Gateway y QA preview](QA_GATEWAY_PREVIEW.md): comandos, EN/JP, threads, permisos, canciones/polls, resultados, reconexión/reinicio, rate limits, login/cookies/perfiles/títulos, HOF poblado y móviles; cada integración real sigue PENDIENTE.
- [Hosting y límites actuales](HOSTING.md): no se seleccionó una opción que garantice 24/7 gratuito sin suspensión. Falta disponibilidad/cuenta/host persistente aprobado.

Errores o límites que persisten: fuente/despliegue real no identificados por red, ausencia de dump definitivo, integraciones Discord/Supabase/Vercel sin credenciales, falta de QA visual y carga WAN, Docker/Linux/ARM y CI remoto no ejecutados. La resolución/republicación de mensajes ambiguos requiere revisión operativa; no se promete entrega exactamente una vez. El código heredado aún contiene excepciones amplias y render Pillow que requieren observación en Gateway real; no se declaran eliminados todos los fallos históricos.

Para declarar READY faltan esas evidencias, escritores antiguos efectivamente detenidos, respaldo final restaurado y ventana de corte/rollback aprobada. Activar Season 5 en producción requiere autorización separada y el script actual seguirá rechazándolo. No hay garantía 24/7 ni lanzamiento completo sin estas verificaciones.
