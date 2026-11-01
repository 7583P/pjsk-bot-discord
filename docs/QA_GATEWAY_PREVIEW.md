# QA de Gateway y preview — pendiente de integración real

Este documento prepara pruebas; ninguna fila implica que se haya ejecutado contra Discord o Vercel. Guardar resultados con fecha, commit/artefacto, operador, entorno y evidencia redactada. No registrar tokens, cookies, códigos OAuth, nombres completos ni dumps de jugadores. Usar cuentas de prueba autorizadas.

## Preparación aislada

Crear una aplicación/guild Discord de pruebas y canales/rangos equivalentes. Completar una copia privada de `deploy/staging-topology.example.json`; no copiar los IDs productivos. Configurar APPLICATION_ID, GUILD_ID, `PJSK_ENV=staging`, `DISCORD_TOPOLOGY_FILE` y la huella de la conexión de staging. El loader mantiene nombres/rangos y reglas; solo cambia destinos Discord. Requiere ambos mapas EN/JP y todos los rangos originales.

Activar Members y Message Content en la aplicación. Comprobar View Channel, Send Messages, Create Public Threads, Send Messages in Threads, Read Message History, Manage Threads y Manage Roles; rol del bot por encima de todos los rangos que debe aplicar. Confirmar las categorías y canales de resultados/boards por servidor. No conceder Administrator para ocultar errores de permisos.

Instalar el servicio como usuario lounge y dar escritura únicamente al directorio assets que usan los helpers originales. Arrancar una instancia; comprobar localhost:8081/health/ready. Una segunda debe fallar por lease. Sin DB/Gateway/recuperación, readiness responde 503; liveness no demuestra READY del lanzamiento.

## Matriz Gateway

| Caso | Acción en staging | Evidencia esperada | Estado real |
|---|---|---|---|
| Slash/texto | Sincronizar comandos con el administrador permitido, comparar fixture original y ejecutar registro, stats, mmr y leaderboard | Nombres/parámetros y respuesta válida; ack antes de operaciones lentas | PENDIENTE |
| EN/JP | Participantes autorizados entran en cada cola y prueban entrar duplicados | Colas separadas; usuario no ocupa dos salas; IDs/roles correctos | PENDIENTE |
| Ventanas | Observar PREP :25–:29/:55–:59 y slots :00/:30 | Grupos, orden y comienzo conforme al original; sin adelantar por reloj visual | PENDIENTE |
| Threads/boards | Crear salas mediante el flujo normal y comprobar miembros, board y cierre | Thread único, categoría/canal correctos y estado DB correspondiente | PENDIENTE |
| Selección/polls | Ejecutar selección, reroll y votos en ambos servidores | Cinco charts, dificultades/brackets/probabilidades originales; no duplicar entrega | PENDIENTE |
| Placement | Retornante y nuevo con MMR inicial 0 juegan su primera partida | SP/Placement visibles; siembra original y eliminación del flag tras confirmar | PENDIENTE |
| Submit | Validar formato, miembros, rechazo, empate y aprobación; reintentar un resultado | Una transacción/resultado y un cambio de MMR; historial/canciones/rangos coherentes | PENDIENTE |
| Roles/permisos | Retirar temporalmente permiso/jerarquía y reponerlos | MMR permanece confirmado; trabajo de rol persiste y luego se resuelve | PENDIENTE |
| REST/rate limit | Revisar backoff bajo fallos controlados; observar métricas reales si aparece 429 | No hacer una tormenta para forzar 429; respetar retry del cliente y trabajos persistentes | PENDIENTE |
| Reconexión | Interrumpir la red del proceso de pruebas y restablecerla | Gateway reconecta; no inicia otro escritor ni repite resultado | PENDIENTE |
| Reinicio | SIGTERM/restart con cola, sala sin voto y con voto/selección en curso | Restauración de estado; ambigüedad → needs_attention; no renovar voto vencido | PENDIENTE |
| Publicación | Fallar una entrega tras confirmar DB y revisar thread/mensaje | pending persiste; asociación/reenvío auditado sin recalcular MMR | PENDIENTE |
| Temporadas | Ensayo local respaldado y resultado retrasado de temporada cerrada | Todo inicia en 0, historia intacta, resultado antiguo rechazado, segundo reset rechazado | LOCAL probado; Gateway pendiente |

Las pruebas automáticas usan reacciones/intercambios Discord simulados y PostgreSQL real local. No certifican entrega, imágenes, límites REST, permisos o reconexión reales. El bot heredado aún contiene tramos de render Pillow y manejos amplios de excepciones; medir latencia/errores durante esta matriz antes de decidir cambios de rendimiento. No se declara solucionado cada fallo histórico por pasar MMR unitario.

## Preview Vercel

1. Ejecutar npm ci/test/build en web y empaquetar desde el backend moderno hacia **un destino nuevo**. La verificación comprueba reproducibilidad de manifiestos y rechaza assets privados/symlinks. Conservar el paquete anterior y su hash; no sobrescribirlo.
2. Crear/configurar preview autorizado con API_DATABASE_URL de staging, huella revisada, OAuth de pruebas, SESSION_SECRET y PJSK_BUILD_ID del artefacto/commit. Sin DISCORD_TOKEN ni variables públicas de DB. El archivo public se sirve por CDN; el Gateway no se ejecuta en Vercel.
3. Comprobar HTTPS válido, `/`, assets locales, `/api/runtime-info` y `/health/ready`. Comparar build ID con la revisión aprobada. Leer temporadas y validar una activa; no usar el contador como evidencia.
4. Probar login/callback en navegador real, recarga y expiración; state de un navegador no debe funcionar en otro ni repetirse. Inspeccionar cookies sin copiarlas: Secure/HttpOnly/Lax, rutas y dominio correctos. Probar logout y rechazo de la sesión presentada previamente.
5. Con dos cuentas de prueba: cada una modifica solo su propio perfil/PFP/título. Intentar ID ajeno, título no ganado y POST cross-origin: deben rechazarse o ignorar IDs no autoritativos. Comprobar vinculación por identidad Discord, sin selección arbitraria de otro jugador.
6. Revisar leaderboard con desempates, perfiles públicos, historial, partidas/canciones/deltas, HOF y cutoffs de una temporada archivada poblada. Cambiar rápidamente de temporada para comprobar que no se aplican respuestas tardías.
7. Móvil/escritorio: probar al menos anchuras 360/390/768/1440, zoom, menús, overlays, login, scroll/tablas y accesibilidad básica. No se cambió CSS; aun así hace falta QA visual real.
8. Probar cookie-only en staging después de la compatibilidad actual. Solo retirarlo del frontend cuando estén verificadas sesiones y logout reales en todos los dominios usados.

Si API y web quedan en el mismo proyecto candidato, conservar rutas relativas sin proxy externo. Si se alojan separados, desde web:

```text
node scripts/configure-proxy.cjs https://BACKEND-VERIFICADO BUILD_ID_REVISADO vercel.https.candidate.json
```

La herramienta exige HTTPS sin credenciales/query, readiness y coincidencia de build/referencia; escribe un archivo nuevo y conserva headers. No modifica vercel.json ni despliega. Un ID declarado por el servidor no es una atestación criptográfica: el administrador debe verificar su host/artefacto y fuente DB. Mantener el mismo backend autoridad de escrituras.

## Monitoreo, incidentes y rollback

Bot: consultar por loopback `/health/live`, `/health/ready`, `/health/diagnostics`; no publicarlos sin un control de acceso. Revisar trabajos de roles, publicaciones pending, rooms needs_attention y agotamiento del pool. API: `/health/ready` usa 503 para DB no disponible; el `/health` histórico conserva su contrato. No interpretar un HTTP 200 de liveness como integración productiva validada.

systemd limita reinicios a cinco en 300 s y espera 20 s entre fallos. Tras reparar una configuración, el operador puede usar reset-failed y arrancar deliberadamente. Docker tiene HEALTHCHECK, pero una marca unhealthy no reinicia por sí sola el contenedor: requiere monitor/supervisor y revisión. Estas configuraciones Linux/ARM/Docker están preparadas y pendientes de ejecución en sus plataformas.

Antes de rollback detener el escritor nuevo, conservar su estado y comparar resultados posteriores al backup. Si hubo escrituras, reconciliarlas antes de recuperar una copia. No reiniciar el viejo escritor en paralelo, no borrar pending para que el tablero parezca sano y no restaurar con DROP/CLEAN sobre producción. Restaurar en otra base autorizada, validar y cambiar conexiones/rutas de forma coordinada.
