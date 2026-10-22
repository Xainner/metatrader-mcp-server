# Plan de mejoras del MCP de MetaTrader 5

Fecha: 21 de octubre de 2026.
Repositorio: https://github.com/Xainner/metatrader-mcp-server
Base del plan: versión 0.6.0 con PR #2 fusionado, commit `6b5867aa5a66904f9c4b9cc810e200ff67171910`.

**Estado: implementado en la versión 0.7.0 y probado con el terminal simulado; validación en cuenta demo pendiente.** Este plan documenta las ocho mejoras acordadas para ejecución, velocidad y confianza operativa. Cada sección incluye ahora un apartado «Implementación» con los módulos, el contrato público y las pruebas. Las casillas marcadas se verificaron con pruebas automáticas contra `FakeMT5` (`tests/unit/`); ninguna se verificó contra un servidor de trading real. Las casillas sin marcar requieren un terminal real, mediciones o trabajo fuera de este repositorio, y así se indica.

Desarrollo completo del plan: [estudio de automatización, señales y centro de operaciones con Rinari Agency](AUTOMATION_IMPLEMENTATION_STUDY.md), con diseño inicial de dos agentes y ampliación a ocho. El [anexo de contratos](SIGNAL_TOOL_CONTRACTS.md) detalla herramientas, permisos y ejemplos propuestos.

El objetivo es reducir llamadas innecesarias, verificar los resultados contra MT5 y mantener un comportamiento definido ante errores. La rentabilidad de una estrategia requiere evaluación independiente; una mejora de infraestructura no demuestra una ventaja de mercado.

## Prioridades y entregas

| ID | Mejora | Prioridad | Entrega |
|---|---|---|---|
| IMP-001 | Conexión recuperable y registros que no bloqueen | Alta | 1 |
| IMP-002 | Snapshot de cuenta y mercado | Alta | 1 |
| IMP-003 | Cálculo monetario y comprobación previa | Alta | 1 |
| IMP-004 | Identidad persistente de solicitudes | Alta | 1 |
| IMP-005 | Confirmación de ejecución y protecciones | Alta | 2 |
| IMP-006 | Límites acumulados y propiedad de estrategia | Alta | 2 |
| IMP-007 | Supervisor local de posiciones | Media, tras 004–006 | 3 |
| IMP-008 | Datos de análisis estructurados e incrementales | Media | 3 |

Las prioridades son orden de trabajo de este plan. No sustituyen las prioridades de incidencias documentadas en [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## IMP-001 — Conexión recuperable y registros que no bloqueen

### Cambios propuestos

- Incorporar al servidor del repositorio el manejo de logs probado en el adaptador local: archivo con rotación y stdout reservado para JSON-RPC. Evitar que una importación de FastMCP deje activo un handler Rich distinto del configurado por el despliegue.
- En Rinari, drenar stderr del subproceso en un lector independiente con almacenamiento acotado. Ese cambio pertenece al transporte de Rinari y requiere trabajo en su repositorio; no se resuelve publicando únicamente este MCP.
- Añadir `get_bridge_health`: versión/commit, cuenta de referencia, conectividad, última lectura exitosa, latencia y errores recientes, sin contraseñas ni otros secretos.
- Definir plazos y estados de salud para conexión, lectura y envío. Una operación nativa de MT5 bloqueada puede requerir aislamiento en un proceso de trabajo; cancelar una tarea async no garantiza que la llamada nativa se haya detenido.
- Recuperar conexiones/procesos con una política acotada. Un envío interrumpido queda pendiente de reconciliación: reiniciar el proceso no autoriza reenviar la orden.
- Evitar que un fallo de logging detenga el recorrido de trading; conservar un diagnóstico acotado de la degradación.

### Aceptación

- [ ] Prueba prolongada con stderr sin consumidor: el servidor continúa respondiendo y los registros no crecen sin límite. *Parcial:* un subproceso con stdout y stderr en tuberías que nadie lee configura el logging, escribe 20 000 registros y termina; los archivos quedan acotados por la rotación (`test_logs_go_to_a_bounded_file_and_never_block_on_an_unread_stderr`). Falta la prueba prolongada del servidor completo con su cliente real.
- [x] Pruebas separadas de desconexión, terminal cerrado, llamada de lectura bloqueada y envío de resultado incierto (simuladas: `test_imp001_bridge.py`).
- [x] Recuperar el canal no genera nuevos envíos de órdenes por sí mismo (`test_failed_reconnection_is_bounded_and_never_runs_the_call`, `test_queued_call_whose_caller_timed_out_is_never_started`).
- [x] Los errores indican etapa y estado conocido; un timeout no se informa como cancelación confirmada del bróker (`test_send_timeout_is_reported_as_unknown_not_cancelled`, `test_read_tool_error_names_stage_and_state`).
- [x] Salud y logs permiten medir el problema sin exponer credenciales ni publicar datos privados de cuentas (login enmascarado, filtro de secretos: `test_health_masks_the_login_and_reports_bridge_and_metrics`, `test_secrets_are_masked_in_log_records`).

### Implementación

- `metatrader_mcp/logging_config.py`: archivo con rotación (`MCP_LOG_FILE`, `MCP_LOG_MAX_BYTES`, `MCP_LOG_BACKUPS`, `MCP_LOG_LEVEL`); elimina los handlers instalados por la importación de FastMCP; stderr solo con `MCP_LOG_STDERR_LEVEL`. Un fallo de escritura se cuenta y se expone, nunca se propaga. Los valores de contraseña y tokens configurados se enmascaran. Lo invocan `metatrader-mcp-server` (`cli.py`) y `server.py` como script.
- `metatrader_mcp/bridge.py`: todas las herramientas son `async` y ejecutan MT5 en un único hilo dedicado, con plazos por etapa (`MCP_READ_TIMEOUT_SECONDS`, `MCP_SEND_TIMEOUT_SECONDS`). Una llamada que supera su plazo sigue en el hilo (la llamada nativa no puede interrumpirse); hasta que termina, las nuevas llamadas fallan de inmediato con «bridge busy» y no se inician. Una llamada en cola cuyo plazo vence antes de empezar no se ejecuta nunca. Antes de cada llamada se comprueba el terminal y, si está desconectado, se reconecta con una política acotada (`MCP_RECONNECT_MAX_ATTEMPTS` por `MCP_RECONNECT_WINDOW_SECONDS`, espera exponencial); reconectar nunca repite una solicitud. El arranque ya no falla si la primera conexión falla.
- Herramienta `get_bridge_health`: versión y commit, cuenta de referencia enmascarada, conectividad, llamada en curso o bloqueada, reconexiones, percentiles p50/p95/p99 por etapa (`read`, `preview`, `send`, `broker_send`, `confirm`), errores recientes, degradación del logging y estado del supervisor. Responde sin esperar al terminal.
- Un envío que vence su plazo devuelve `status: "no_response"`, `verification: "unknown"`, `stage` y el `request_id` generado por el servidor para consultarlo después.
- **Fuera de este repositorio:** drenar stderr del subproceso en el transporte de Rinari sigue pendiente en su propio repositorio.

## IMP-002 — Snapshot de cuenta y mercado

### Cambios propuestos

Crear `get_trading_snapshot(symbol)` con:

- Bid, ask, spread en precio/puntos, hora del tick y antigüedad calculada.
- Estado de cuenta, moneda, equity, margen y permisos.
- Posiciones y pendientes del símbolo, tickets, volúmenes y P&L flotante.
- Límites efectivos, exposición y riesgo disponible, o estado explícito de cálculo no disponible.
- Hora de inicio/fin de captura, hora/fuente por componente y errores parciales.

Usar MT5 como fuente del estado de cuenta. Los datos de otras fuentes deben identificarse con su proveedor y no reemplazar silenciosamente los valores ausentes de la cuenta. El snapshot reúne lecturas sucesivas: no prometer que sean una transacción atómica del bróker.

### Aceptación

- [x] Un error de lectura produce `unknown`/error explícito, nunca un listado vacío o P&L cero inventado (`test_snapshot_read_error_is_explicit_never_an_empty_list_or_zero_pnl`).
- [x] Los consumidores pueden comprobar frescura y desfase entre componentes: `captured_at` y `duration_ms` por componente, `capture.max_skew_ms`, `age_seconds` del tick cuando se configura `MT5_SERVER_UTC_OFFSET_SECONDS` (sin ella la edad no se inventa).
- [x] Los campos monetarios declaran moneda y alcance: posición, estrategia o cuenta.
- [ ] Medir llamadas y latencia frente a consultar por separado; no fijar una mejora porcentual antes de medir. *Instrumentado* (`capture.terminal_calls`, `duration_ms`, métricas por etapa); la medición con un terminal real está pendiente.
- [x] La ejecución revalida la cotización y el estado crítico aunque el análisis use un snapshot anterior: `send_order` vuelve a leer la cotización y aplica los límites al enviar; con `preview_id`, un movimiento mayor que el permitido rechaza la orden.

### Implementación

`order/get_trading_snapshot.py`, herramienta `get_trading_snapshot(symbol, strategy_id?)`, REST `GET /api/v1/order/snapshot/{symbol}`. Componentes: `quote`, `account`, `positions`, `pending_orders`, `limits` (configuración efectiva, bloqueo de nuevas entradas y uso actual: volumen, huecos, reservas, pérdida máxima por operación) y `capture`. Cada componente tiene `status`, `source: "mt5"` y `captured_at`; uno ilegible tiene `status: "error"` y valores `None`, y `error` de la respuesta es verdadero aunque los demás se devuelvan. Las posiciones incluyen `ownership` respecto a la estrategia.

## IMP-003 — Cálculo monetario y comprobación previa

### Herramientas propuestas

- `get_symbol_specifications`: dígitos, point/tick size, valor/tamaño de contrato disponible, límites y paso de volumen, distancias de stops, modos de ejecución/llenado y permisos.
- `calculate_trade_outcome`: estimación monetaria con `order_calc_profit`, expresada en la moneda de la cuenta.
- `calculate_required_margin`: estimación con `order_calc_margin`.
- `preview_order`: solicitud normalizada, entrada, SL/TP, volumen, riesgo monetario estimado, relación beneficio/riesgo, margen, exposición resultante y resultado de `order_check`.

Usar parámetros del símbolo concreto del bróker; no asumir especificaciones genéricas de XAUUSD para XAUUSDm. Identificar costes conocidos y desconocidos, y separar estimación del resultado real. Definir caducidad del preview y revalidar condiciones al ejecutar. Un preview aprobado no reserva un precio ni garantiza ejecución.

### Aceptación

- [x] Casos BUY/SELL, distintas monedas y especificaciones de contrato (simulados: signo, moneda de la cuenta y tamaño de contrato por símbolo; la conversión real entre monedas la hace `order_calc_profit` del terminal y no se validó contra un servidor).
- [x] Error o dato insuficiente bloquea una ejecución que dependa de ese cálculo (`test_unavailable_loss_calculation_blocks_the_preview`, `test_calculation_failure_is_an_error`; los límites de riesgo ya bloqueaban sin cálculo).
- [x] Riesgo calculado para el volumen final ajustado al paso válido, sin aumentar silenciosamente el riesgo solicitado (`test_risk_sizing_rounds_down_and_never_exceeds_the_amount`).
- [x] `order_check` se aplica a la solicitud que se pretende enviar; cambios materiales invalidan el preview (`order/_build.py` construye la misma solicitud; `test_material_change_after_preview_is_rejected`).
- [x] Mercado que se mueve entre preview y ejecución produce revalidación o rechazo conforme a límites configurados (`test_price_move_after_preview_is_rejected`, `test_expired_preview_is_rejected`).

### Implementación

- `get_symbol_specifications` (`market/get_symbol_specifications.py`): atributos del símbolo concreto, modos decodificados, distancias de stops y congelación, llenado por defecto y `unavailable`.
- `calculate_trade_outcome` y `calculate_required_margin` (`order/`): `order_calc_profit` / `order_calc_margin`, en la moneda de la cuenta, con alcance y costes excluidos declarados.
- `preview_order` (`order/preview_order.py`): `volume` o `risk_amount` (volumen máximo cuyo riesgo no supera el importe, redondeado hacia abajo); pérdida en SL, beneficio en TP, relación, margen, exposición, límites de riesgo, `order_check`, coste de spread conocido y costes desconocidos. Si todo pasa devuelve `preview_id`, `expires_at`, `max_price_move_points` y `send_with` (herramienta y argumentos). Un preview se usa una vez (`order/_preview_store.py`). Configuración: `MT5_PREVIEW_TTL_SECONDS` (30), `MT5_PREVIEW_MAX_PRICE_MOVE_POINTS` (por defecto, la desviación de la orden: 20 puntos), `MT5_REQUIRE_PREVIEW` (false).
- REST: `GET /market/specifications/{symbol}`, `POST /order/calculate/profit`, `POST /order/calculate/margin`, `POST /order/preview`.

## IMP-004 — Identidad persistente de solicitudes

### Cambios propuestos

- Aceptar un `request_id` estable por intención de operación y guardar su huella de parámetros normalizados en un registro durable, por ejemplo SQLite.
- Asociar ID a cuenta/servidor, estrategia, símbolo, solicitud, intentos, resultados y tickets de orden/deal/posición.
- Registrar la intención antes de enviar y mantener estados como `prepared`, `sending`, `accepted`, `partially_filled`, `filled`, `rejected`, `unknown` y `reconciled`, con semántica documentada.
- Repetir el mismo ID devuelve el estado conocido; reutilizarlo con otros parámetros devuelve conflicto. Una nueva operación intencional requiere un ID distinto.
- Ofrecer `get_request_status` y reconciliación por historial y estado de cuenta tras desconexiones o reinicios.
- Mantener la ventana temporal de duplicados como protección adicional configurable. El despliegue local usa actualmente 3 segundos; ese ajuste no debe convertirse accidentalmente en un valor universal del repositorio.

### Aceptación

- [x] Mismo ID simultáneo y mismo ID tras reinicio no producen un segundo envío automático (`test_same_request_id_concurrently_sends_once`, `test_same_request_id_after_a_restart_is_not_sent_again`).
- [x] Probar caída antes del envío, durante él y después de que el bróker ejecute pero antes de guardar el resultado (`test_crash_before_sending_is_reported_as_not_sent`, `test_crash_during_the_send_blocks_the_symbol_until_resolved`, `test_crash_after_execution_is_reconciled_from_the_history`).
- [x] Un resultado incierto queda bloqueado para reenvío hasta resolverlo o aplicar una decisión explícita y auditable: el mismo ID nunca se reenvía y, con `MT5_BLOCK_ON_UNRESOLVED=true` (por defecto), las nuevas aperturas del mismo símbolo se bloquean hasta `reconcile_request` o `resolve_request` con motivo.
- [x] No prometer ejecución exactamente una vez: documentado en `requests_registry.py`, en las herramientas y aquí.
- [x] Documentar retención, concurrencia y migración del registro sin guardar secretos (`store.py`, `requests_registry.py`).

### Implementación

- `metatrader_client/store.py`: SQLite (`MT5_STATE_DB`, por defecto `~/.metatrader_mcp/state.sqlite3`; `state_db` en la configuración del cliente) con migraciones por `PRAGMA user_version`, WAL y transacciones `BEGIN IMMEDIATE`, de modo que varios procesos sobre el mismo archivo se serializan. No guarda credenciales: login y servidor, símbolos, volúmenes, precios, tickets y mensajes del bróker.
- `metatrader_client/requests_registry.py`: estados `prepared`, `sending`, `accepted`, `partially_filled`, `filled`, `rejected`, `unknown` y `reconciled` (con `outcome`), con eventos. La intención se registra antes de cualquier comprobación y `sending` se confirma antes de `order_send`; así `prepared` abandonado (más de `MT5_REQUEST_STALE_SECONDS`, 120 s) significa «no enviado» y `sending` abandonado pasa a `unknown`. Cada orden lleva en el comentario la etiqueta `r<hash>` del ID (31 caracteres como máximo) para reconciliarla; si el bróker reemplaza los comentarios, la reconciliación usa los tickets o queda `unknown`. Retención: `MT5_REQUEST_RETENTION_DAYS` (90) sobre estados finales; `unknown`, `sending` y `prepared` nunca se purgan.
- Las aperturas (`place_market_order`, `place_pending_order`) siempre tienen `request_id` (generado si no se pasa); los cierres, cuando se pasa. La respuesta incluye `request_id`, `request_state` y, al repetir el ID, `replayed: true` con `previous_result`.
- Herramientas `get_request_status`, `list_requests`, `reconcile_request` (busca órdenes, posiciones, historial de órdenes y deals por ticket o etiqueta; nunca envía) y `resolve_request` (`not_executed` o `accept_unknown`, motivo obligatorio). REST bajo `/api/v1/order/requests`.
- La ventana temporal de duplicados (`MT5_DUPLICATE_WINDOW_SECONDS`) se mantiene como protección adicional con su valor por defecto del repositorio (10 s); el despliegue local conserva su propio ajuste.

## IMP-005 — Confirmación de ejecución y protecciones

### Cambios propuestos

- Uniformar el contrato de resultados en cliente, MCP y REST, conservando retcode, mensaje del bróker, IDs y evidencia de verificación.
- Separar solicitud aceptada, ejecución parcial, ejecución completa y estado incierto. El mensaje humano debe coincidir con el estado; `placed` no debe mostrarse como `Filled`.
- Tras una apertura, reconciliar orden/deals/posición y comprobar volumen y SL/TP efectivos. Si la protección falta, comunicarlo con un estado explícito y seguir una política configurada; no cerrar o modificar posiciones automáticamente sin esa política.
- Tras modificaciones, comprobar los niveles finales; tras cierres, comprobar el volumen restante. Mantener un resultado por ticket en operaciones masivas.
- Registrar precio solicitado frente al ejecutado, coste conocido, duración y deslizamiento observado; declarar unidades y limitaciones de la medida.

### Aceptación

- [x] Aceptación sin fill, fill parcial, publicación tardía, rechazo, timeout y fallo de verificación conservan estados distintos (`verification`: `pending`, `partial`, `unknown`, ausente en rechazos, `unknown` con `no_response`, `mismatch`; pruebas en `test_imp005_006_execution_limits.py`).
- [x] Un SL/TP solicitado pero ausente no devuelve «posición protegida» (`protection.status: "missing"`, `verification: "protection_missing"` y aviso en el mensaje).
- [x] Posiciones y órdenes que se actualizan con desfase no liberan exposición no reconciliada (reservas por volumen de MT5-003, sin cambios; sus pruebas siguen pasando).
- [x] La respuesta incluye qué se comprobó, cuándo y qué sigue desconocido (`checks.checked_at`, `checks.verified`, `checks.unknown`, `checks.details`).
- [x] Cambios compatibles o migración documentada para consumidores de `error`, `status` y `verification` actuales (ver «Migración» abajo).

### Implementación

- `order/_confirm.py`: tras una apertura de mercado se consultan el deal (precio ejecutado, comisión, cargo), la posición (volumen y SL/TP efectivos) y se calcula el deslizamiento en puntos (positivo = peor que el precio pedido) y los tiempos `send_ms`/`confirm_ms`. Las pendientes se comprueban en el listado de órdenes; las modificaciones vuelven a leer niveles; las cancelaciones, que la orden ya no esté.
- Mensajes: `placed` de una orden de mercado se informa como «Accepted … not filled yet», nunca como «Filled».
- Política ante una protección ausente (`MT5_MISSING_PROTECTION_POLICY`): `report` (por defecto, solo informa), `retry_sltp` (envía el SL/TP una vez y lo verifica) o `close` (cierra y verifica). Sin configurarla no se modifica ni se cierra nada automáticamente.
- Las operaciones masivas mantienen un resultado por ticket con su `verification`.

### Migración

- `error`: en aperturas sigue significando «el bróker ejecutó o aceptó la orden»; la confirmación posterior está en `verification`, porque convertir un fallo de verificación en `error: true` invitaría a reenviar una orden ejecutada. En `modify_position`, `modify_pending_order` y `cancel_pending_order`, `error: false` ahora exige `verification: "confirmed"` (antes bastaba el `retcode`), como ya ocurría en `close_position`.
- `status` no cambia. Campos nuevos: `request_id`, `request_state`, `replayed`, `previous_result`, `checks`, `execution`, `protection`, `protection_action`.
- `verification` sigue siendo una cadena; en cierres conserva `confirmed`, `remaining`, `unknown` y `not_closed`. Las herramientas MCP masivas exigen `scope` (ver IMP-006); la biblioteca y REST lo mantienen opcional por compatibilidad.

## IMP-006 — Límites acumulados por estrategia y cuenta

### Cambios propuestos

- Propagar `strategy_id`, `magic` y referencias de solicitud a las herramientas de apertura, consulta y gestión.
- Diferenciar operaciones propias, manuales y de otras estrategias. El uso de un magic number ayuda a identificar, pero no constituye por sí mismo una frontera de autorización.
- Aplicar límites configurables de volumen, entradas, exposición monetaria, riesgo agregado al SL y pérdida diaria, contemplando pendientes que puedan activarse.
- Definir el tratamiento de posiciones sin SL, monedas, costes, exposición opuesta y cuentas netting/hedging. Evitar sumar o compensar riesgos sin una regla documentada.
- Exigir alcance explícito en cierres/cancelaciones masivas y ofrecer un resumen previo de tickets afectados.
- Separar la desactivación de nuevas entradas de las operaciones autorizadas para reducir exposición.

### Aceptación

- [x] Varias entradas pequeñas y pendientes no evitan el límite acumulado (`test_small_entries_and_pending_orders_count_towards_the_daily_entries_limit`, `test_aggregate_risk_includes_positions_pending_orders_and_the_new_order`).
- [x] Las operaciones de otra estrategia no se modifican por un filtro ambiguo (`test_single_ticket_of_another_strategy_is_not_changed`, `test_bulk_close_only_touches_the_requested_scope`, `test_ambiguous_bulk_scope_is_rejected`).
- [x] Pruebas concurrentes y de datos ilegibles bloquean aperturas cuyo riesgo no se pueda evaluar (`test_concurrent_orders_of_a_strategy_cannot_exceed_its_limit`, `test_unreadable_positions_block_strategy_limits`, `test_position_without_stop_loss_makes_the_aggregate_risk_unknown`).
- [x] Límites efectivos visibles y persistentes; modificarlos deja registro (`get_risk_limits`; cada conjunto distinto de límites efectivos se guarda al arrancar; el interruptor de entradas guarda cada cambio con motivo).
- [x] Los valores los define el usuario o una política explícita; el plan no prescribe un tamaño ni una pérdida aceptable (todos los límites nuevos están desactivados por defecto).

### Implementación

- `strategy_id` en aperturas, modificaciones, cierres y consultas; el `magic` se deriva de `MT5_STRATEGY_MAGICS` (JSON) o de un hash estable del ID (`metatrader_client/strategy.py`). Propiedad: `own`, `manual` (magic 0) u `other`. Un magic identifica, no autoriza.
- Límites nuevos en `risk.py`: `MT5_MAX_ENTRIES_PER_DAY` (deals de entrada del día del servidor + pendientes que pueden activarse + reservas no listadas), `MT5_MAX_AGGREGATE_RISK_PCT` (suma de pérdidas hasta el SL de posiciones, pendientes, reservas y la nueva orden, sin compensar exposición opuesta; un elemento sin SL bloquea) y `MT5_STRATEGY_LIMITS` (JSON por estrategia con `max_volume_per_order`, `max_total_volume`, `max_open_positions`, `max_daily_loss`, `max_entries_per_day`, `max_aggregate_risk_pct`, aplicados además de los de cuenta). En cuentas *netting* cada posición es neta por símbolo; la regla de suma se aplica igual y queda documentada.
- Operaciones masivas: `scope` (`strategy`, `manual`, `all`) obligatorio en MCP, `dry_run` lista los tickets afectados sin enviar.
- Interruptor de nuevas entradas: `MT5_ALLOW_NEW_ENTRIES` (configuración) y `disable_new_entries(reason)` (persistente). Cerrar, cancelar y modificar stops siguen disponibles. Reactivar no se expone como herramienta MCP: queda para el operador (`MT5Order.enable_new_entries`, REST `POST /order/entries/enable`).
- Cierres hechos por el servidor usan el magic de la posición, para que la pérdida diaria por estrategia incluya sus cierres.
- Reservas compartidas: las reservas de exposición viven en la tabla `reservations` del Store (migración 3), por cuenta y servidor. Antes de `order_send`, los límites que dependen de reservas (posiciones abiertas, volumen total, entradas del día y riesgo agregado) se vuelven a evaluar y la reserva se inserta en una única transacción `BEGIN IMMEDIATE`, así que dos conexiones o procesos que comparten la base no pueden usar la misma parte del presupuesto. La reserva cubre el envío pendiente, el resultado incierto (durante el TTL) y la publicación tardía. No coordina procesos con otra base, la operativa manual ni otros robots.

## IMP-007 — Supervisor local de posiciones

### Cambios propuestos

- Ofrecer reglas explícitas y persistentes para tickets/estrategias concretos, por ejemplo cierre cuando el P&L alcance un umbral definido por el usuario.
- Proponer `create_position_rule`, `list_position_rules`, `pause_position_rule` y `remove_position_rule` con alcance, moneda, condición, acción y estado verificables.
- Ejecutar la comprobación local con intervalo configurable y medición de antigüedad del dato; no depender de que el modelo conteste en cada ciclo.
- Coordinar cada acción con el registro de solicitudes, la verificación de ejecución y una única instancia propietaria de la regla.
- Mostrar regla activa, pausada, disparada, ejecutándose, cumplida, fallida o pendiente de reconciliación. Definir qué ocurre al reiniciar o desconectarse.
- Mantener los SL/TP del bróker como protecciones independientes; un supervisor local depende de conexión/proceso y no garantiza un precio o beneficio exacto.

### Aceptación

- [x] Alta, pausa y eliminación requieren una petición explícita; instalar la función no activa reglas (`test_without_rules_the_supervisor_does_nothing`, `test_rule_lifecycle_requires_explicit_requests`).
- [x] Dos ciclos o procesos no duplican el cierre de la misma regla (arrendamiento en la base compartida + compare-and-set + `request_id` estable por cierre: `test_two_supervisors_never_close_twice`, `test_second_owner_waits_for_the_lease`).
- [x] Reinicio, fill parcial o resultado desconocido no provocan reintentos ciegos (`test_interrupted_rule_is_not_retried_after_a_restart`, `test_partial_close_is_pending_reconciliation`, `test_uncertain_or_rejected_close_is_not_retried`).
- [x] Un dato de P&L obsoleto o ilegible no se sustituye por una cotización externa (`test_stale_or_unreadable_pnl_is_not_evaluated`).
- [x] Registrar umbral observado, hora, acción y resultado final; distinguir P&L flotante del neto realizado (`result.observed`, `results[].floating_pnl_at_trigger`, `results[].realized_net_close` y `results[].realized_net_position`).

### Implementación

`metatrader_client/supervisor.py`, herramientas `create_position_rule`, `list_position_rules`, `pause_position_rule`, `resume_position_rule`, `remove_position_rule` (REST `/api/v1/rules`). Reglas por ticket o por estrategia (opcionalmente un símbolo), métrica `pnl` (beneficio + swap) o `profit`, operador `>=`/`<=`, acción `close`. Estados: `active`, `paused`, `triggered`, `executing`, `fulfilled`, `failed`, `pending_reconciliation`, `target_closed`, `removed`, con eventos. El servidor MCP arranca el bucle (`MT5_SUPERVISOR_ENABLED`, `MT5_SUPERVISOR_INTERVAL_SECONDS`, `MT5_SUPERVISOR_MAX_STALE_SECONDS`) en el mismo hilo serializado que las herramientas; sin reglas activas no consulta MT5. El servidor REST no ejecuta el bucle: las reglas creadas por REST las evalúa un servidor MCP con el supervisor activo sobre la misma base de estado. Frescura: con `MT5_SERVER_UTC_OFFSET_SECONDS` se calcula a partir de la hora del tick; sin ella, la cotización solo cuenta como fresca después de que este proceso vea cambiar la hora del tick, y deja de serlo si no cambia durante `MT5_SUPERVISOR_MAX_STALE_SECONDS`. La primera observación tras arrancar nunca prueba frescura. La recuperación de reglas `triggered`/`executing` de otro propietario solo ocurre dentro de `run_cycle` y con el lease en manos de este supervisor (al menos 180 s, más que un cierre); construir un supervisor no cambia nada. El neto se informa por cierre (`realized_net_close`, solo el deal de este cierre) y por posición (`realized_net_position`: todos los deals, con la comisión de entrada y los cierres parciales anteriores). Los SL/TP del bróker siguen siendo la protección independiente.

## IMP-008 — Datos de análisis estructurados e incrementales

### Cambios propuestos

- Añadir `closed_only=true` para señales y un indicador explícito de vela cerrada/en formación.
- Entregar JSON con esquema y timestamps UTC; ofrecer CSV como compatibilidad/exportación, evitando que el agente deba interpretar tablas ambiguas.
- Crear una consulta de varias temporalidades, por ejemplo M1/M5/H4, con resultados y errores por temporalidad.
- Añadir actualizaciones incrementales por timestamp y límites de tamaño/paginación. Corregir huecos, duplicados y actualizaciones de la vela actual.
- Cachear historia cerrada con política de invalidación y procedencia; no reutilizar sin comprobación una cotización o snapshot de riesgo para ejecutar.
- Si se añaden indicadores, devolver fórmula, parámetros, ventana y versión; no producir una puntuación de confianza comercial sin validación empírica.

### Aceptación

- [x] La vela en formación queda excluida cuando se solicita `closed_only`.
- [x] Orden cronológico, huso horario, huecos y semántica de volumen son explícitos (`order`, `time_basis`, `gaps`, `volume_semantics`).
- [x] La consulta incremental coincide con una consulta completa equivalente (`test_incremental_query_matches_the_full_query`).
- [x] Una temporalidad fallida no convierte toda la respuesta en una apariencia de éxito completo (`test_multi_timeframe_reports_errors_per_timeframe`).
- [ ] Medir tamaño de respuesta, llamadas y latencia antes/después con cargas representativas. *Instrumentado* (`terminal_calls`, `duration_ms`, `cache.fetched_bars`); la medición con cargas reales está pendiente.

### Implementación

`market/get_candles.py`, herramientas `get_candles` y `get_candles_multi`, REST `GET /market/candles` y `GET /market/candles/multi`. Esquema `candles/v1`: barras ascendentes sin duplicados, `closed` por barra (cierre ≤ hora del último tick; sin tick solo la última se considera en formación; con el mercado cerrado la última puede figurar en formación de forma conservadora), `time` del servidor y `time_iso` en UTC si se configura `MT5_SERVER_UTC_OFFSET_SECONDS` (si no, `time_basis: "server"`). Incremental con `since=next_since`, `limit` y `truncated` (máximo `MT5_MAX_CANDLES`, 5000); la vela en formación se devuelve de nuevo en cada consulta y debe reemplazarse. Caché en memoria solo de velas cerradas, verificada en cada uso contra la última barra del terminal e invalidada si difiere; cotizaciones y datos de riesgo nunca se sirven de caché. `format: "csv"` como exportación. No se añadieron indicadores.

## Validación transversal y métricas

- Mantener las regresiones de [KNOWN_ISSUES.md](KNOWN_ISSUES.md), especialmente respuestas ilegibles, concurrencia y llenados parciales RETURN.
- Probar primero con simuladores; las pruebas que envíen órdenes requieren una cuenta demo dedicada y activación deliberada.
- Medir p50/p95/p99 de lectura, preview, envío y confirmación por separado. El tiempo del modelo y la ejecución del bróker deben distinguirse del tiempo del MCP. *Instrumentado:* `get_bridge_health.metrics` separa `read`, `preview`, `send` (herramienta completa), `broker_send` (`order_send`) y `confirm`; faltan las mediciones con un terminal real.
- Ejecutar pruebas prolongadas de transporte, reinicios y datos tardíos; conservar resultados reproducibles y errores, no solo el número de pruebas aprobadas.
- Evaluar calidad de ejecución con deslizamiento, rechazos, fills parciales, protecciones efectivas y resultados inciertos. Evaluar la estrategia con un registro separado y costes explícitos.
- Antes de actualizar el despliegue, documentar versión/commit, cambios de esquema y procedimiento de recuperación. Conservar la configuración local de cuenta demo y los límites elegidos.

## Referencias oficiales

- [Especificaciones del símbolo](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py).
- [Cálculo de beneficio/pérdida en moneda de cuenta](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py).
- [Cálculo de margen](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcmargin_py).
- [Comprobación previa de solicitud](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py).
- [Envío y resultado de una operación](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py).
- [Velas y barra actual](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py).

## Seguimiento de entregas

- [x] Entrega 1: IMP-001, IMP-002, IMP-003 e IMP-004 — implementadas y probadas con simulador (pendiente: drenaje de stderr en Rinari, prueba prolongada del servidor, mediciones reales).
- [x] Entrega 2: IMP-005 e IMP-006 — implementadas y probadas con simulador.
- [x] Entrega 3: IMP-007 e IMP-008 — implementadas y probadas con simulador (pendiente: mediciones de rendimiento con cargas reales).
- [ ] Validación de las tres entregas en una cuenta demo dedicada.

Cada mejora se cierra con implementación, pruebas de aceptación, contrato público actualizado y evidencia del alcance realmente validado. La implementación no activa reglas del supervisor ni envía operaciones por sí misma; no se ejecutó contra un terminal ni una cuenta.

### Revisión del PR #3

La revisión del head `6d74418` reprodujo cuatro hallazgos con `FakeMT5`. Se corrigieron así; sus cuatro reproducciones y cinco casos complementarios están en `tests/unit/test_review_regressions.py`:

- [x] **P1:** el riesgo agregado podía superarse entre conexiones que comparten el Store, porque las reservas eran locales de cada guard. Ahora las reservas son durables y compartidas, y la comprobación final y la reserva son atómicas entre conexiones y procesos (ver IMP-006). Casos cubiertos: dos guards concurrentes, dos `Store` sobre el mismo archivo y un envío incierto visto desde otra conexión.
- [x] **P1:** el primer tick visto se consideraba fresco aunque fuera antiguo. Ahora la frescura exige un cambio observado o la edad calculada con el desfase del servidor.
- [x] **P2:** construir otro supervisor movía a `pending_reconciliation` una regla que otro propietario con lease válido seguía ejecutando. Ahora la recuperación solo ocurre con el lease adquirido y para reglas de otro propietario. Si un resultado llega tarde, se guarda en los eventos de la regla.
- [x] **P2:** el neto realizado omitía la comisión de entrada. Ahora se calcula sobre todo el historial de la posición y aparte para el deal de este cierre.

### Segunda revisión del PR #3

La revisión del head `593b9b2` encontró otro P1. La comprobación final reutilizaba las posiciones leídas antes del commit. Si en ese intervalo otro lector ve publicada una posición y borra su reserva por reconciliación, la exposición desaparece a la vez de las posiciones leídas y del libro de reservas. Secuencia: lectura A → envío B → publicación B → snapshot/reconciliación B → commit A.

- [x] Cada libro de reservas (cuenta y servidor) tiene una versión (`reservation_versions`, migración 4) que sube con cada reserva guardada, actualizada, liberada por reconciliación o vencida. El plan guarda la versión con la que se reconciliaron sus posiciones. Si al hacer el commit la versión cambió, las posiciones se vuelven a leer y los límites se reevalúan, hasta `MAX_COMMIT_ATTEMPTS` (3) veces; si la versión sigue cambiando, la orden se rechaza sin enviarse. Pruebas en `tests/unit/test_review_r2_extra.py`: la reproducción del revisor, el rechazo con la razón real (21 frente al límite de 15), la reevaluación que sí envía cuando queda margen, el rechazo tras intentos acotados y el aumento de versión al vencer una reserva.

### Tercera revisión del PR #3

En el head `4b0cf6e` la versión del libro se tomaba después de leer posiciones y órdenes en MT5. Por eso podía asociar una lectura antigua a la versión que ya incluía el borrado de otra reserva: lectura A sin posiciones → envío, publicación y reconciliación de B → A toma la versión nueva y confirma sin volver a leer.

- [x] Ahora la versión se toma **antes** de leer el terminal. Al reconciliar, el plan solo sigue siendo válido si el libro conserva esa versión; si no, queda marcado como obsoleto (`STALE_VERSION`) y el commit vuelve a leerlo todo, con intentos acotados. Los cambios que hace la propia reconciliación del plan se siguen, así que no provocan relecturas innecesarias. Pruebas en `tests/unit/test_review_r3_extra.py`: la reproducción del revisor, un cambio durante la lectura de órdenes, un cambio durante la lectura de revalidación y la comprobación de que la reconciliación propia no fuerza una relectura.

### Evidencia de esta implementación

- `pytest`: 263 pruebas aprobadas y 39 de integración omitidas tras la tercera revisión (259 tras la segunda, 254 tras la primera, 245 en el head inicial, 146 antes del plan). Las pruebas nuevas están en `tests/unit/test_imp001_bridge.py`, `test_imp002_003_snapshot_preview.py`, `test_imp004_requests.py`, `test_imp005_006_execution_limits.py` y `test_imp007_008_supervisor_candles.py`.
- Pruebas existentes ajustadas al contrato nuevo: número y esquemas de herramientas MCP, argumentos de identidad reenviados por MCP y REST, y la prueba de reservas MT5-003 con resultado desconocido, que ahora desactiva `block_on_unresolved` para aislar la capa de reservas (con el bloqueo activo, la segunda orden se rechaza antes).
- `FakeMT5` aplica ahora las modificaciones SLTP/MODIFY aceptadas (la verificación nueva detectaba, correctamente, que antes no lo hacía) y simula `order_check`, `order_calc_margin`, historial de órdenes y velas.
- Arranque real por STDIO sin credenciales: `initialize`, `tools/list` (45 herramientas) y `get_bridge_health` responden; el log se escribe en el archivo configurado.
