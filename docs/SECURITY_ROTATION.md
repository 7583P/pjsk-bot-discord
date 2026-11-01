# Credenciales y rotación operativa

Estado: procedimiento preparado; no se accedió a cuentas ni se rotaron credenciales durante esta sesión. No compartir valores por chat, commits, capturas, URLs o logs. La aplicación usa PostgreSQL desde servidor; no necesita una Supabase Service Role Key en el navegador.

## Token del bot comprometido

1. Identificar la aplicación existente y sus procesos Python/Node/Heroku/Railway/VM. Detenerlos y deshabilitar sus reinicios cuando sea posible. Una lease del código nuevo no impide que escriba un proceso antiguo.
2. Entrar como propietario en [Discord Developer Portal](https://discord.com/developers/applications), elegir la aplicación correcta, abrir **Bot → Reset Token**, confirmar y completar 2FA si lo solicita. Copiar el valor una sola vez directamente al gestor de secretos o al archivo privado del servicio. Discord documenta este flujo de regeneración en su [soporte oficial](https://support-dev.discord.com/hc/en-us/articles/6470840524311-Why-can-t-I-copy-my-bot-s-token).
3. Configurar `DISCORD_TOKEN` únicamente en el host del bot nuevo. No actualizar con él los escritores antiguos ni las variables de Vercel. Conservar `APPLICATION_ID` y verificar la aplicación elegida; para QA usar una aplicación/guild separados.
4. En Linux utilizar el editor privado del administrador (`sudoedit /etc/pjsk-lounge.env`), propietario root y permisos 600; el servicio systemd carga el archivo antes de cambiar a usuario lounge. En Windows usar el editor/gestor de entorno privado. No ejecutar `echo`, imprimir el entorno ni poner el token como argumento de un comando.
5. Ejecutar `python -m scripts.check_environment bot`. Solo devuelve nombres de variables faltantes y categorías de error. Rechaza hashes de tokens comprometidos conocidos. Esto no comprueba revocación en Discord ni valida un token por red.
6. Tras validar staging, arrancar **una sola** instancia. Revisar identidad de la aplicación, intents, jerarquía de roles y salud privada. No arrancar una copia antigua si falla el token; corregir configuración y límites de reinicio del supervisor.
7. Revisar auditoría de Discord por acciones no esperadas y localizar otras copias del secreto. Limpiar historial requiere una operación coordinada posterior: esta entrega conserva el historial y no hace force-push. Borrar texto de la última versión no sustituye la rotación.

## OAuth y sesiones

Variables de API: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, `FRONTEND_URL`, `SESSION_SECRET` y `API_DATABASE_URL`. El callback HTTPS exacto debe estar registrado en **OAuth2 → Redirects** de la aplicación correspondiente. En preview, usar dominio y aplicación de pruebas, sin callbacks apuntando a producción.

Generar SESSION_SECRET mediante el gestor de contraseñas/secretos con suficiente entropía (al menos 32 caracteres; preferible 48 bytes aleatorios), sin imprimirlo. Si una clave anterior es segura, conservarla durante la transición mantiene sesiones compatibles. Si estuvo comprometida, rotarla e invalidar las sesiones anteriores deliberadamente.

Las cookies son Secure, HttpOnly y SameSite=Lax; las escrituras exigen Origin válido o Bearer conforme a la política del servidor. El estado OAuth queda ligado al navegador, consume una sola vez y expira. Logout revoca los tokens presentados. No asumir revocación global de todas las sesiones de una cuenta.

Mantener `AUTH_COOKIE_ONLY=false` hasta completar QA HTTPS: login, recarga, perfil, títulos, logout y dominios. Después probar `true` en staging: no hay token en el fragmento ni en `authToken` (se conserva la clave vacía por compatibilidad). No se eliminó localStorage del cliente ni soporte Bearer prematuramente.

`python -m scripts.check_environment api` comprueba las variables y la coherencia HTTPS del callback sin conectar ni mostrar valores. No configurar `DISCORD_TOKEN` en este servicio.

## PostgreSQL, builds y Git

Mantener credenciales administrativas únicamente para inspección/migración/backup; bot con `lounge_bot` y API con `lounge_api`. Ninguno debe ser propietario de tablas, superuser ni BYPASSRLS. No usar prefijos de frontend como VITE_/NEXT_PUBLIC_ para URLs privadas. No incorporar `.env`, dumps, llaves privadas o copias de perfiles a `public/`.

Antes de cada commit ejecutar el escáner del candidato contra cada repositorio y luego contra los blobs staged: `python -m scripts.secret_scan --repo RUTA --staged`. El escáner informa rutas/categorías, nunca valores. Comprueba patrones de Discord/JWT/llaves/URLs y archivos privados; no certifica detectar todos los posibles secretos. Los placeholders de pruebas y ejemplos deben ser explícitos.

`PJSK_BUILD_ID` es un identificador público de commit/artefacto revisado, nunca un secreto. `/api/runtime-info` ayuda a comparar un despliegue con el candidato; un endpoint público que declara una versión no prueba por sí solo identidad de datos ni integridad del host.
