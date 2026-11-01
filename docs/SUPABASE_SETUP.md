# Preparación de Supabase y verificación de la fuente

Ningún proyecto Supabase fue creado o modificado. Las pruebas usan PostgreSQL local y fixtures sintéticos; esos fixtures **no se importan a Supabase**. La fuente definitiva sigue pendiente de acceso autorizado.

## Proyectos y credenciales

1. El propietario crea proyectos distintos para staging y producción, comprueba región/capacidad/cuotas y registra sus referencias. En Free hay un límite publicado de dos proyectos activos y 500 MB por base; medir los datos reales antes de decidir. [Supabase: planes](https://supabase.com/pricing).
2. Guardar contraseña administrativa, referencias y cadenas de conexión en el gestor privado. En **Connect**, copiar los endpoints reales: session/direct para bot y herramientas persistentes; transaction para API. No deducir hosts de la región. El pool transaction de Supavisor requiere desactivar prepared statements; el candidato ya usa `statement_cache_size=0` y pool API 1. [Conexiones oficiales](https://supabase.com/docs/guides/database/connecting-to-postgres).
3. No habilitar acceso de navegador a las tablas. La API usa conexión PostgreSQL de servidor y OAuth Discord; no requiere entregar Service Role Key al frontend. `service_role` puede saltarse RLS y no debe sustituir al login limitado. [Roles de Supabase](https://supabase.com/docs/guides/database/postgres/roles).

## Fuente e integridad antes de migrar

Con una conexión administrativa autorizada a la fuente, configurar `API_DATABASE_URL` privadamente y ejecutar el inspector en modo de entorno `PJSK_ENV=inspection`:

```text
python -m scripts.inspect_source
```

El programa hace una transacción de solo lectura con snapshot estable. Devuelve metadata, índices, constraints, políticas, recuentos, temporadas y huérfanos agregados; no devuelve filas de jugadores ni DSN. Conserva la salida como evidencia privada. Comprobar que los recuentos no estén filtrados por RLS; las cuentas limitadas no equivalen a un inventario administrativo completo.

Identificar en el host autorizado el proceso escritor, configuración DB efectiva, versión de código, supervisor y posibles trabajadores antiguos. El hash de conexión del inspector sirve para comparar el destino revisado; no descubre por sí solo cuál es producción. Contrastarlo con configuración del servicio y un dump restaurado. No inferir población desde la SQLite de 58 jugadores.

Exportar también el DDL real mediante pg_dump `--schema-only --schema=public --no-owner --no-acl`, usando las credenciales mediante entorno privado/archivo de autenticación protegido, nunca un URI con contraseña en argumentos. Revisar extensiones, tipos, defaults, FK, índices, triggers, secuencias y esquemas externos a public. El inspector no reemplaza ese dump.

## Restore y migraciones

1. Obtener un backup consistente; para corte final detener todos los escritores, incluidas mutaciones del backend antiguo. Restaurar primero en una base local vacía. `scripts.backup_postgres` compara todas las tablas públicas usando pg_dump/pg_restore y hashes; requiere clientes compatibles. Configurar `RESTORE_DATABASE_URL` solo hacia `localhost/pjsk_restore_*`. Archivos/Storage/esquemas fuera de public necesitan respaldo adicional. Supabase recomienda exportación externa para Free. [Backups oficiales](https://supabase.com/docs/guides/platform/backups).
2. Resolver en staging, con registro del operador, los huérfanos/duplicados que reporte el diagnóstico. No borrar filas automáticamente para que pase un índice. Verificar `players.user_id` bigint para el bot; la tolerancia TEXT de la API no prueba compatibilidad del bot con otro tipo.
3. Restaurar la fuente verificada en el proyecto de staging y aplicar `python -m scripts.migrate_runtime --apply` con login administrativo. Sin --apply no cambia nada. Se aplican `001`–`007` en transacción con checksums; el arranque no migra ni resetea. No cargar `tests/fixtures/current_schema.sql` en el proyecto.
4. Revisar FK/constraints reales junto a las comprobaciones lógicas del inspector. Los controles de transacción/participantes del servicio no convierten las relaciones históricas sin FK en constraints validadas. Registrar cualquier diferencia y repetir la suite sobre la restauración; no declarar READY mientras falte esa comprobación.

## Logins limitados

Las migraciones crean roles de servidor NOLOGIN. Después de revisar grants, el administrador puede habilitar LOGIN para `lounge_bot` y `lounge_api`, manteniendo NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOREPLICATION/NOBYPASSRLS. Desde una sesión psql privada utilizar `\password lounge_bot` y `\password lounge_api` para introducir contraseñas sin incluirlas en scripts ni historial de comandos.

Configurar los usuarios de conexión correspondientes en los endpoints copiados del panel; en Supavisor el formato incluye la referencia de proyecto. Comprobar la autenticación real y `SELECT current_user` en cada servicio: no basta que funcione con postgres y SET ROLE. Esta autenticación a Supavisor está pendiente de acceso real.

Comprobar en staging que API puede leer rankings y escribir perfiles/OAuth, pero no actualizar MMR; bot puede confirmar resultados sin ownership ni BYPASSRLS. Verificar RLS, políticas, privilegios heredados, límites del pool y ausencia de grants para anon/authenticated en estas tablas. La suite local ya prueba los permisos mediante roles PostgreSQL reales; Supabase añade una integración pendiente.

## Aislamiento de staging

Configurar `PJSK_ENV=staging` y `STAGING_DATABASE_FINGERPRINT` en cada servicio. Obtener la huella previamente con el inspector, en modo inspection, usando exactamente el URL/login/endpoint que utilizará ese servicio de staging. Bot y API pueden necesitar huellas diferentes porque sus conexiones son distintas. La huella no incluye contraseña; cambiarla no exige exponerla.

El arranque consulta la conexión y rechaza una huella distinta **antes de tomar lease o escribir**. Si cambian host, puerto, login o backend de PostgreSQL, volver a verificar el destino antes de actualizarla. Esto detecta errores de configuración, pero no sustituye la identificación administrativa de una base ni protege contra un operador que aprueba deliberadamente el destino equivocado.

Para el bot de staging también son obligatorios `DISCORD_TOPOLOGY_FILE`, GUILD_ID y APPLICATION_ID de pruebas, con todos los IDs EN/JP/rangos del archivo ejemplo. El loader rechaza el guild productivo, mapas incompletos, colas mezcladas y overrides fuera de staging. El comportamiento por defecto conserva los IDs originales.

El proyecto de producción permanece sin escrituras del candidato. Repetir restore, diagnóstico, permisos, backups y corte conforme a [CURRENT_DEPLOYMENT.md](CURRENT_DEPLOYMENT.md). Activar Season 5 exige autorización separada; el script actual rechaza producción incluso con credenciales válidas.
