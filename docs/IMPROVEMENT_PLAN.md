# Plan de mejoras del MCP de MetaTrader 5

Fecha: 21 de octubre de 2026.
Repositorio: https://github.com/Xainner/metatrader-mcp-server
Base del plan: versión 0.6.0 con PR #2 fusionado, commit `6b5867aa5a66904f9c4b9cc810e200ff67171910`.

**Estado: propuesto; implementación pendiente.** Este plan documenta las ocho mejoras acordadas para ejecución, velocidad y confianza operativa. Los nombres nuevos de herramientas y los campos descritos son propuestas de contrato, no capacidades disponibles todavía.

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

- [ ] Prueba prolongada con stderr sin consumidor: el servidor continúa respondiendo y los registros no crecen sin límite.
- [ ] Pruebas separadas de desconexión, terminal cerrado, llamada de lectura bloqueada y envío de resultado incierto.
- [ ] Recuperar el canal no genera nuevos envíos de órdenes por sí mismo.
- [ ] Los errores indican etapa y estado conocido; un timeout no se informa como cancelación confirmada del bróker.
- [ ] Salud y logs permiten medir el problema sin exponer credenciales ni publicar datos privados de cuentas.

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

- [ ] Un error de lectura produce `unknown`/error explícito, nunca un listado vacío o P&L cero inventado.
- [ ] Los consumidores pueden comprobar frescura y desfase entre componentes.
- [ ] Los campos monetarios declaran moneda y alcance: posición, estrategia o cuenta.
- [ ] Medir llamadas y latencia frente a consultar por separado; no fijar una mejora porcentual antes de medir.
- [ ] La ejecución revalida la cotización y el estado crítico aunque el análisis use un snapshot anterior.

## IMP-003 — Cálculo monetario y comprobación previa

### Herramientas propuestas

- `get_symbol_specifications`: dígitos, point/tick size, valor/tamaño de contrato disponible, límites y paso de volumen, distancias de stops, modos de ejecución/llenado y permisos.
- `calculate_trade_outcome`: estimación monetaria con `order_calc_profit`, expresada en la moneda de la cuenta.
- `calculate_required_margin`: estimación con `order_calc_margin`.
- `preview_order`: solicitud normalizada, entrada, SL/TP, volumen, riesgo monetario estimado, relación beneficio/riesgo, margen, exposición resultante y resultado de `order_check`.

Usar parámetros del símbolo concreto del bróker; no asumir especificaciones genéricas de XAUUSD para XAUUSDm. Identificar costes conocidos y desconocidos, y separar estimación del resultado real. Definir caducidad del preview y revalidar condiciones al ejecutar. Un preview aprobado no reserva un precio ni garantiza ejecución.

### Aceptación

- [ ] Casos BUY/SELL, distintas monedas y especificaciones de contrato.
- [ ] Error o dato insuficiente bloquea una ejecución que dependa de ese cálculo.
- [ ] Riesgo calculado para el volumen final ajustado al paso válido, sin aumentar silenciosamente el riesgo solicitado.
- [ ] `order_check` se aplica a la solicitud que se pretende enviar; cambios materiales invalidan el preview.
- [ ] Mercado que se mueve entre preview y ejecución produce revalidación o rechazo conforme a límites configurados.

## IMP-004 — Identidad persistente de solicitudes

### Cambios propuestos

- Aceptar un `request_id` estable por intención de operación y guardar su huella de parámetros normalizados en un registro durable, por ejemplo SQLite.
- Asociar ID a cuenta/servidor, estrategia, símbolo, solicitud, intentos, resultados y tickets de orden/deal/posición.
- Registrar la intención antes de enviar y mantener estados como `prepared`, `sending`, `accepted`, `partially_filled`, `filled`, `rejected`, `unknown` y `reconciled`, con semántica documentada.
- Repetir el mismo ID devuelve el estado conocido; reutilizarlo con otros parámetros devuelve conflicto. Una nueva operación intencional requiere un ID distinto.
- Ofrecer `get_request_status` y reconciliación por historial y estado de cuenta tras desconexiones o reinicios.
- Mantener la ventana temporal de duplicados como protección adicional configurable. El despliegue local usa actualmente 3 segundos; ese ajuste no debe convertirse accidentalmente en un valor universal del repositorio.

### Aceptación

- [ ] Mismo ID simultáneo y mismo ID tras reinicio no producen un segundo envío automático.
- [ ] Probar caída antes del envío, durante él y después de que el bróker ejecute pero antes de guardar el resultado.
- [ ] Un resultado incierto queda bloqueado para reenvío hasta resolverlo o aplicar una decisión explícita y auditable.
- [ ] No prometer ejecución exactamente una vez: el bróker puede no ofrecer una clave idempotente y puede existir una ventana cuyo resultado no se determine automáticamente.
- [ ] Documentar retención, concurrencia y migración del registro sin guardar secretos.

## IMP-005 — Confirmación de ejecución y protecciones

### Cambios propuestos

- Uniformar el contrato de resultados en cliente, MCP y REST, conservando retcode, mensaje del bróker, IDs y evidencia de verificación.
- Separar solicitud aceptada, ejecución parcial, ejecución completa y estado incierto. El mensaje humano debe coincidir con el estado; `placed` no debe mostrarse como `Filled`.
- Tras una apertura, reconciliar orden/deals/posición y comprobar volumen y SL/TP efectivos. Si la protección falta, comunicarlo con un estado explícito y seguir una política configurada; no cerrar o modificar posiciones automáticamente sin esa política.
- Tras modificaciones, comprobar los niveles finales; tras cierres, comprobar el volumen restante. Mantener un resultado por ticket en operaciones masivas.
- Registrar precio solicitado frente al ejecutado, coste conocido, duración y deslizamiento observado; declarar unidades y limitaciones de la medida.

### Aceptación

- [ ] Aceptación sin fill, fill parcial, publicación tardía, rechazo, timeout y fallo de verificación conservan estados distintos.
- [ ] Un SL/TP solicitado pero ausente no devuelve «posición protegida».
- [ ] Posiciones y órdenes que se actualizan con desfase no liberan exposición no reconciliada.
- [ ] La respuesta incluye qué se comprobó, cuándo y qué sigue desconocido.
- [ ] Cambios compatibles o migración documentada para consumidores de `error`, `status` y `verification` actuales.

## IMP-006 — Límites acumulados por estrategia y cuenta

### Cambios propuestos

- Propagar `strategy_id`, `magic` y referencias de solicitud a las herramientas de apertura, consulta y gestión.
- Diferenciar operaciones propias, manuales y de otras estrategias. El uso de un magic number ayuda a identificar, pero no constituye por sí mismo una frontera de autorización.
- Aplicar límites configurables de volumen, entradas, exposición monetaria, riesgo agregado al SL y pérdida diaria, contemplando pendientes que puedan activarse.
- Definir el tratamiento de posiciones sin SL, monedas, costes, exposición opuesta y cuentas netting/hedging. Evitar sumar o compensar riesgos sin una regla documentada.
- Exigir alcance explícito en cierres/cancelaciones masivas y ofrecer un resumen previo de tickets afectados.
- Separar la desactivación de nuevas entradas de las operaciones autorizadas para reducir exposición.

### Aceptación

- [ ] Varias entradas pequeñas y pendientes no evitan el límite acumulado.
- [ ] Las operaciones de otra estrategia no se modifican por un filtro ambiguo.
- [ ] Pruebas concurrentes y de datos ilegibles bloquean aperturas cuyo riesgo no se pueda evaluar.
- [ ] Límites efectivos visibles y persistentes; modificarlos deja registro.
- [ ] Los valores los define el usuario o una política explícita; el plan no prescribe un tamaño ni una pérdida aceptable.

## IMP-007 — Supervisor local de posiciones

### Cambios propuestos

- Ofrecer reglas explícitas y persistentes para tickets/estrategias concretos, por ejemplo cierre cuando el P&L alcance un umbral definido por el usuario.
- Proponer `create_position_rule`, `list_position_rules`, `pause_position_rule` y `remove_position_rule` con alcance, moneda, condición, acción y estado verificables.
- Ejecutar la comprobación local con intervalo configurable y medición de antigüedad del dato; no depender de que el modelo conteste en cada ciclo.
- Coordinar cada acción con el registro de solicitudes, la verificación de ejecución y una única instancia propietaria de la regla.
- Mostrar regla activa, pausada, disparada, ejecutándose, cumplida, fallida o pendiente de reconciliación. Definir qué ocurre al reiniciar o desconectarse.
- Mantener los SL/TP del bróker como protecciones independientes; un supervisor local depende de conexión/proceso y no garantiza un precio o beneficio exacto.

### Aceptación

- [ ] Alta, pausa y eliminación requieren una petición explícita; instalar la función no activa reglas.
- [ ] Dos ciclos o procesos no duplican el cierre de la misma regla.
- [ ] Reinicio, fill parcial o resultado desconocido no provocan reintentos ciegos.
- [ ] Un dato de P&L obsoleto o ilegible no se sustituye por una cotización externa.
- [ ] Registrar umbral observado, hora, acción y resultado final; distinguir P&L flotante del neto realizado.

## IMP-008 — Datos de análisis estructurados e incrementales

### Cambios propuestos

- Añadir `closed_only=true` para señales y un indicador explícito de vela cerrada/en formación.
- Entregar JSON con esquema y timestamps UTC; ofrecer CSV como compatibilidad/exportación, evitando que el agente deba interpretar tablas ambiguas.
- Crear una consulta de varias temporalidades, por ejemplo M1/M5/H4, con resultados y errores por temporalidad.
- Añadir actualizaciones incrementales por timestamp y límites de tamaño/paginación. Corregir huecos, duplicados y actualizaciones de la vela actual.
- Cachear historia cerrada con política de invalidación y procedencia; no reutilizar sin comprobación una cotización o snapshot de riesgo para ejecutar.
- Si se añaden indicadores, devolver fórmula, parámetros, ventana y versión; no producir una puntuación de confianza comercial sin validación empírica.

### Aceptación

- [ ] La vela en formación queda excluida cuando se solicita `closed_only`.
- [ ] Orden cronológico, huso horario, huecos y semántica de volumen son explícitos.
- [ ] La consulta incremental coincide con una consulta completa equivalente.
- [ ] Una temporalidad fallida no convierte toda la respuesta en una apariencia de éxito completo.
- [ ] Medir tamaño de respuesta, llamadas y latencia antes/después con cargas representativas.

## Validación transversal y métricas

- Mantener las regresiones de [KNOWN_ISSUES.md](KNOWN_ISSUES.md), especialmente respuestas ilegibles, concurrencia y llenados parciales RETURN.
- Probar primero con simuladores; las pruebas que envíen órdenes requieren una cuenta demo dedicada y activación deliberada.
- Medir p50/p95/p99 de lectura, preview, envío y confirmación por separado. El tiempo del modelo y la ejecución del bróker deben distinguirse del tiempo del MCP.
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

- [ ] Entrega 1: IMP-001, IMP-002, IMP-003 e IMP-004.
- [ ] Entrega 2: IMP-005 e IMP-006, apoyadas en identidad persistente y cálculo monetario.
- [ ] Entrega 3: IMP-007 e IMP-008, con mediciones de rendimiento y pruebas de recuperación.

Cada mejora se cierra con implementación, pruebas de aceptación, contrato público actualizado y evidencia del alcance realmente validado. Este commit añade únicamente el plan; no activa vigilancia ni envía operaciones.
