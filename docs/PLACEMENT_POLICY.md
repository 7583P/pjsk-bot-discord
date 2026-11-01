# Season 5: 0 inicial y Placement original

Decisión confirmada por el usuario el 2026-11-01: **«0 inicial y Placement original»**. Corrige la interpretación anterior que deshabilitaba ambos mecanismos. No se ejecutó ningún reset productivo.

| Alternativa | MMR al activar | Primera partida | Estado |
|---|---|---|---|
| Reset original `8a4f812` | Retornantes desde `max(0, anterior−50)` | Season Placement / tabla Placement originales | No cumple 0 inicial para todos |
| Iron 0 sin Placement | Todos 0 | Pairwise desde 0 sin siembra | Interpretación anterior retirada; no estaba autorizada |
| **0 inicial y Placement original** | Todos 0 en `players` y `seasonplayers` | Conserva siembra, prioridad y empates originales | **Confirmada; implementada y ensayada localmente** |
| Base 0 también en primera partida | Todos 0 | Modifica el cálculo inicial y potencialmente la clasificación | No seleccionada |

El procedimiento moderno conserva la elegibilidad del `/seasonreset` auténtico: un jugador que tiene registro estacional previo y MMR positivo, flag Season Placement, starting MMR positivo o historial previo entra en Season Placement. Los demás conservan Placement normal. El helper histórico `/newseason` tenía un criterio más estrecho; no se lo usa como reemplazo silencioso del reset moderno.

Al activar, todos tienen MMR 0, cero partidas/puntos/W-L y ningún resultado nuevo. Los retornantes tienen `is_season_placement=true`, `starting_mmr=0` y rango base Iron; la UI muestra **Season Placement, Starting MMR 0**. Los demás tienen `role=Placement`. Los nuevos registros posteriores también empiezan en Placement 0.

El primer resultado vuelve a aplicar las reglas originales: retornante desde `max(0, MMR anterior−50)`; Placement normal desde su tabla de PGGBM (por ejemplo AP → 800), y luego pairwise/empates/clamp originales. Por ello el primer historial puede tener `mmr_before > 0` aunque el reset almacenó 0. No significa que se haya importado una partida anterior a Season 5.

Las proyecciones efectivas usadas por el bot para clasificar colas y seleccionar canciones mantienen la regla previa. Algunas tarjetas/nombres de sala pueden mostrar ese valor proyectado antes del primer submit; no se alteró el emparejador para forzar que toda proyección visual sea 0. El MMR canónico de la temporada y el Starting MMR que entrega la API sí son 0 inicialmente. Esta distinción conserva las probabilidades y agrupaciones originales.

Tras confirmar la primera partida, se limpia el flag en la misma transacción del resultado. Un rol Discord atrasado no puede volver a sembrar MMR: el servicio toma el estado de PostgreSQL. La conciliación y el reingreso al servidor conservan el rol Season Placement mientras no haya una partida confirmada.

La política del ensayo se registra como `zero_start_preserve_placement`, con `placement_enabled=true`. Las migraciones versionadas existentes no fueron reescritas. Los ensayos previos con `full_zero/false` no se convierten ni se promueven: repetir sobre otra restauración aislada. Producción continúa bloqueada por el script.
