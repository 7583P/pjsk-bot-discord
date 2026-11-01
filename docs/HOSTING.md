# Hosting con presupuesto S/0

Consulta de documentación oficial: 2026-11-01. No se crearon cuentas, recursos ni planes de pago. Ningún proveedor fue verificado con una carga real de PJSK.

La opción preparada es una VM persistente para el Gateway, Supabase PostgreSQL y API/web en Vercel. Su viabilidad con S/0 depende de disponibilidad, límites y condiciones de las cuentas; no permite prometer 24/7 gratuito.

Oracle Always Free permite VM en la región principal, sujeto a capacidad disponible. Su documentación actual indica para A1 1500 OCPU-h y 9000 GB-h mensuales, equivalentes a 2 OCPU/12 GB para tenancy gratuita. Puede recuperar instancias consideradas inactivas durante siete días. Una VM pequeña no tiene garantía de permanecer asignada; no generar carga artificial para evitar esa política. [Oracle: recursos Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

Supabase Free publica 500 MB de base y pausa por una semana de inactividad. Medir tamaño real, historial, índices, almacenamiento y tráfico antes del corte; no asumir que los jugadores históricos locales dimensionan producción. [Supabase: límites del plan](https://supabase.com/pricing).

Para conexiones persistentes IPv4, la opción documentada es Supavisor session; para serverless, transaction con pool pequeño. El candidato usa máximo 1 conexión API y `statement_cache_size=0`, con TLS verificado. Los hosts/puertos se copian del panel, sin deducirlos de la región. [Supabase: conexiones PostgreSQL](https://supabase.com/docs/guides/database/connecting-to-postgres).

Vercel documenta FastAPI con assets en `public/` servidos por CDN. El paquete local sigue esa separación. El Gateway Discord permanece en la VM. Falta verificar el proyecto real, sus condiciones/cuotas y el build remoto; el paquete local no prueba un despliegue. [Vercel: FastAPI y archivos estáticos](https://vercel.com/docs/frameworks/backend/fastapi).

Si no hay VM gratuita disponible o se exceden las cuotas, hace falta otro host persistente autorizado o cambiar el presupuesto. No se activó ningún recurso facturable ni se implementaron pings de keep-alive.

## Selección operativa pendiente

| Opción | Límite comprobado | Decisión |
|---|---|---|
| Oracle Always Free VM | Capacidad por región y posible recuperación por inactividad | Candidata condicional; no garantiza el requisito de permanencia |
| Railway Free | La documentación vigente publica USD 1 de crédito mensual tras el trial | No asumir que cubre un worker permanente con carga real |
| Equipo/VM persistente ya disponible del administrador | Depende de energía, red, suspensión del sistema y mantenimiento propios | Alternativa sin tarifa adicional de hosting si el propietario la tiene y valida disponibilidad |

Railway documenta trial de hasta 30 días/USD 5 y después crédito Free mensual; no equivale a una garantía de ejecución gratuita continua. [Railway: Free Trial](https://docs.railway.com/pricing/free-trial).

No se dependerá de un servicio que duerma el Gateway indefinidamente. Como tampoco hay capacidad/cuenta autorizada confirmada, la elección de hosting sigue bloqueando READY. Verificar VM real, arquitectura ARM/x86, RAM bajo render, conexiones Discord/PostgreSQL TLS, reinicio, almacenamiento y recuperación; no crear carga artificial para alterar criterios de inactividad. No se contrató ni seleccionó un plan facturable.
