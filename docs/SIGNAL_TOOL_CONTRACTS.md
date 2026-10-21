# Contratos propuestos de herramientas: señales, Agency y ejecución

Fecha: 21 de octubre de 2026. **Especificación de diseño; ninguna herramienta nueva de este documento se da por implementada.** Complementa [el estudio de automatización](AUTOMATION_IMPLEMENTATION_STUDY.md).

## 1. Convenciones comunes

- Transporte MCP con salida estructurada JSON y una representación textual compatible. `schema_version: "1"` en contratos nuevos. Fechas RFC 3339 UTC; precios/volúmenes/importes como cadenas decimales validadas, convertidas en el adaptador MT5 de forma controlada.
- Toda respuesta incluye `trace_id`, `as_of`, `status`, `data`, `errors` y referencias relevantes. En consultas parciales: timestamp/error por componente y `status=partial`.
- Los nombres `observer`, `analyst`, `decision_maker`, `executor`, `operator` son capacidades del principal autenticado; no un campo libre que el llamante pueda elegir. Los roles no implican jerarquía automática: el operador no necesita un permiso de enviar órdenes para configurar fuentes.
- Escrituras requieren autorización por cuenta/estrategia/política, `request_id` para la acción lógica y control de versión cuando proceda. Mismo ID/contenido devuelve el estado guardado; ID/contenido distinto produce `IDEMPOTENCY_CONFLICT`.
- `retryable=true` permite repetir una consulta o consultar estado según `next_action`; nunca autoriza por sí solo a reenviar una orden.
- Un resultado incierto es un estado de negocio durable, no solo una excepción MCP. Errores de formato/autorización se rechazan antes de cualquier llamada al bróker.
- Los límites de tamaño, paginación y lote son configuración acotada del servicio; rechazar números no finitos, rangos inválidos y campos desconocidos. No aceptar Python, SQL ni expresiones arbitrarias de ejecución.

Excepción de representación: un digest/lote vacío destinado a la Agency instalada debe devolver contenido textual vacío y `structuredContent` ausente o vacío (`{}`, `[]`, `""` o null), sin el envoltorio general no vacío. Si devuelve metadatos JSON, el consumidor actual los convierte en texto y no considera el resultado silencioso. Los metadatos de esa consulta quedan disponibles en salud/auditoría por separado.

Ejemplo de envoltorio propuesto, con datos sintéticos:

```json
{
  "schema_version": "1",
  "trace_id": "trace-example-001",
  "as_of": "2026-10-21T12:00:00Z",
  "status": "ok",
  "data": {"signal_id": "sig-example-001", "revision": 1},
  "errors": []
}
```

Error:

```json
{
  "schema_version": "1",
  "trace_id": "trace-example-002",
  "as_of": "2026-10-21T12:00:03Z",
  "status": "execution_unknown",
  "data": {"request_id": "req-example-001", "reconciliation_required": true},
  "errors": [{
    "code": "BROKER_OUTCOME_UNKNOWN",
    "stage": "send",
    "retryable": false,
    "next_action": "get_execution_status",
    "message": "El envío no tiene resultado confirmado. No reenviar."
  }]
}
```

## 2. Objetos de dominio

### Evento original

Campos obligatorios: `event_id`, `source_id`, `source_kind`, `external_channel_id`, `external_message_id`, `event_type`, `revision`, `content_hash`, `received_at`, `raw_content_ref`. `emitted_at`, `edited_at`, `reply_to_id` y `forward_origin` pueden ser desconocidos con razón explícita.

`event_type`: `new`, `edit`, `delete`, `condition_trigger`, `execution_update`. La identidad externa no es global: siempre se particiona por fuente/canal. El receptor autenticado fija la procedencia; un webhook no puede declarar que proviene de otro canal autorizado. Mantener secuencia/revisión y eventos de borrado sin confiar en que todos los borrados sean observables.

### Señal normalizada

Campos: `signal_id`, `revision`, `root_event_id`, `source_id`, `action`, `canonical_symbol`, `broker_symbol`, `direction`, `entry`, `stop_loss`, `take_profits`, `expires_at`, `correlation`, `extraction`, `state` y `validation_errors`.

- `action`: `open`, `update_protection`, `close_partial`, `close`, `cancel_pending`, `informational`.
- `entry.kind`: `market`, `limit`, `stop`, `zone`; los parámetros dependen del tipo, con esquema discriminado. Para un update no debe exigirse una entrada ficticia.
- Precios absolutos con `unit=price`; `pips`, puntos y distancias solo tras una conversión explícita sustentada en specs. No mezclar estas unidades.
- `take_profits`: precios y fracciones opcionales; la política define asignaciones si el proveedor solo da niveles. Suma de fracciones no excede 1.
- `extraction`: parser/version, evidencias de campos y certeza de extracción opcional; no almacenar una supuesta probabilidad de rentabilidad.
- `correlation`: grupo raíz y razón verificable. Una acción de gestión sin grupo inequívoco queda en cuarentena.

Ejemplo sintético de extracción, **no una señal ni precio actual**:

```json
{
  "signal_id": "sig-example-001",
  "revision": 1,
  "root_event_id": "evt-example-001",
  "source_id": "telegram-example",
  "action": "open",
  "canonical_symbol": "XAUUSD",
  "broker_symbol": "XAUUSDm",
  "direction": "buy",
  "entry": {"kind": "zone", "low": "2500.00", "high": "2502.00", "unit": "price"},
  "stop_loss": {"price": "2495.00", "unit": "price"},
  "take_profits": [{"price": "2510.00", "unit": "price"}],
  "expires_at": "2026-10-21T12:02:00Z",
  "correlation": {"root_signal_id": "sig-example-001", "reason": "new_root_message"},
  "extraction": {"parser": "example-template", "version": "1", "evidence_event_id": "evt-example-001"},
  "state": "parsed",
  "validation_errors": []
}
```

### Política

Identidad/version, cuenta vinculada, demo exigida, fuentes/estrategias/símbolos, actores/capacidades, límites monetarios/agregados, criterios de datos/entrada/SL/TP, horarios y conflictos. `enabled=false` inicialmente. Una concesión de ejecución contiene alcance y vencimiento; activar con configuración incompleta produce `POLICY_INCOMPLETE`.

Separar `mode=observe|shadow|demo_execute`, `entries_enabled`, `protective_management_enabled` y estado de salud. `observe`: ingiere/clasifica; `shadow`: prepara/simula decisiones con datos reales; ambos prohíben escrituras al bróker. No introducir real por un valor libre de modo durante la fase demo.

### Candidato, decisión y plan

`Candidate`: referencia señal/revisión o disparador de estrategia, evidencias de datos, alcance, acción propuesta, `expires_at`. `Decision`: candidato, `approve|wait|reject`, motivos, versión de política y autor autenticado. Una aprobación no omite preflight ni garantiza que el bróker ejecute.

`TradePlan`: `plan_id`, hash, cuenta, estrategia, señal/revisión, decisión o mandato determinista, política/version, expiración, acciones/patas, precios, volúmenes, SL/TP, riesgo estimado y condiciones de ejecución. Inmutable; cambios generan un plan nuevo. La reserva final se realiza en la ejecución bajo transacción.

### Resultado de ejecución

`request_id`, `plan_id`, `state`, cuenta/estrategia/grupo, retcode/mensaje original, volumen solicitado/ejecutado/restante/pendiente, tickets de órdenes/deals/position identifiers, precio medio, protecciones solicitadas/observadas, timestamps y estado de reconciliación.

Estados de ejecución: `prepared`, `reserved`, `submitting`, `confirmed`, `partial`, `placed`, `rejected`, `execution_unknown`, `reconciling`, `cancelled`. Posición/grupo tiene su propio ciclo `open`, `partially_closed`, `closed`; no confundir ciclo de una solicitud con ciclo completo de una operación.

## 3. Catálogo completo propuesto

“Lectura” significa sin cambio de estado de negocio; métricas operativas no convierten una consulta en consumo de eventos. La autorización y el acceso a datos privados se aplican incluso a consultas.

### Salud, mercado y riesgo

| Herramienta | Entradas principales | Salida/semántica | Capacidad |
|---|---|---|---|
| `get_bridge_health` | alcance opcional | Versión, conectividad, owner del runtime, colas, último éxito, incertidumbres; sin secretos | observer, lectura |
| `get_trading_snapshot` | símbolo, alcance | Cuenta, tick, posiciones/pendientes y exposición; timestamp/error por componente | observer, lectura |
| `get_symbol_specs` | símbolo | Contrato, unidades, volumen, stops/freeze, filling, modo y antigüedad | observer, lectura |
| `get_market_context` | símbolo, timeframes, count, closed_only | OHLC e indicadores permitidos/versionados; calidad/frescura por timeframe | observer, lectura |
| `get_candles_incremental` | símbolo, timeframe, after_bar_time, limit, closed_only | Barras ordenadas y cursor reproducible, huecos detectados; no consume cursor global | observer, lectura |
| `get_effective_policy` | policy_id, alcance | Límites y permisos efectivos, versión, estado; campos confidenciales filtrados | observer, lectura |
| `calculate_trade_risk` | símbolo, dirección, entrada/SL, presupuesto o volumen, policy_id | Volumen admisible y pérdida/margen estimados, redondeo, moneda y supuestos | analyst, lectura/cálculo |
| `preview_order` | intención tipada, policy_id | Validación con datos actuales y `order_check`, condiciones/errores; no envía ni reserva | analyst, lectura/cálculo |

`calculate_trade_risk` no acepta presupuesto superior al mandato. Si el lote mínimo excede el riesgo permitido, rechaza. `preview_order` y `prepare_trade_plan` reutilizan el mismo validador; no duplicar reglas divergentes.

### Señales y receptores

| Herramienta | Entradas principales | Salida/semántica | Capacidad |
|---|---|---|---|
| `get_signal_sources` | filtros/cursor | Fuentes, habilitación, salud, lag y versión de parser; no credenciales | observer, lectura |
| `configure_signal_source` | configuración, expected_version, request_id | Alta/revisión deshabilitada inicialmente; referencias a secretos y mapeos explícitos | operator, mutación |
| `ingest_signal_event` | evento, request_id | Persiste procedencia autenticada y deduplica; no abre operaciones | receptor autenticado, mutación |
| `parse_signal` | event_id, parser_version, request_id | Persiste extracción y revisión; no habilita ejecución | analyst/receptor, mutación |
| `validate_signal` | signal_id, revision, policy_id | Informe de elegibilidad con timestamp; no reserva ni acusa evento | analyst, lectura/cálculo |
| `list_signals` | filtros, cursor, limit | Página estable de estados/revisiones; no consume | observer, lectura |
| `get_signal` | signal_id, revision opcional | Revisiones, correlación y trazabilidad; texto original sujeto a permiso | observer, lectura |
| `peek_signal_digest` | consumer_id, profile_id, limit | Novedades posteriores al offset; texto vacío cuando no hay; sin avanzar offset | observer, lectura |
| `ack_signal_events` | consumer_id, event_ids, outcome_refs, request_id | Acuse explícito con razón durable; no afirma que haya operación ejecutada | consumidor autorizado, mutación |
| `evaluate_signal_batch` | policy_id, mode, max_items, request_id | Reclama lote con lease, evalúa, persiste resultados; puede ejecutar solo con mandato válido | analyst en shadow; executor en demo_execute, mutación |
| `replay_signals` | dataset_ref, rango, policy_version, estrategia, request_id | Job aislado y métricas reproducibles; acceso al bróker de escritura prohibido | analyst, mutación local |

El receptor continuo utiliza la misma API interna que `ingest_signal_event`; no necesita pasar cada mensaje por un modelo ni por una sesión MCP. Los cursores y leases pertenecen al servicio; no mantenerlos solo en memoria del agente.

Particionar cursores, leases y resultados por consumidor, política/version y modo. Un lote `shadow` no consume el trabajo de `demo_execute` ni convierte sus aprobaciones en permisos de envío. Al habilitar ejecución no recuperar aperturas antiguas de shadow: volver a comprobar revisión, vigencia y datos actuales o esperar señales nuevas.

`evaluate_signal_batch` resuelve internamente reclamación y acuse con transacciones; no exponer una secuencia frágil “listar y luego abrir” como único camino automático. Para lotes vacíos la representación textual compatible con Agency es vacía. Mantener `request_id` distinto por invocación lógica; la idempotencia de cada señal/acción es adicional a la del lote.

En una regla de Agency con argumentos fijos no puede reutilizarse un `request_id` literal para todos los ticks. Definir un `invocation_id` verificado proporcionado por el adaptador, o permitir omitir el ID de lote y generarlo en el servicio, conservando idempotencia persistente por intención. **Para esta herramienta, el ID de lote puede omitirse bajo esa segunda modalidad; las acciones de bróker siempre tienen ID durable antes del envío.** No permitir omitir IDs de órdenes por esta excepción.

### Decisiones y ejecución

| Herramienta | Entradas principales | Salida/semántica | Capacidad |
|---|---|---|---|
| `submit_candidate` | candidato, request_id | Identidad/version y estado; dedupe por origen/revisión/estrategia | analyst, mutación |
| `get_candidate` | candidate_id | Evidencias, revisión vigente, decisiones y referencias a planes; lectura con alcance | observer, lectura |
| `record_decision` | candidate_id/version, decisión, expected_version, request_id | Transición CAS con autor derivado del principal y vencimiento | decision_maker, mutación |
| `prepare_trade_plan` | decision_id o mandato determinista, policy_id, request_id | Plan inmutable, hash, preflight y caducidad; aún no envía | decision_maker/executor autorizado, mutación local |
| `get_trade_plan` | plan_id | Plan, hash, vencimiento y estado de ejecución asociado | observer con alcance, lectura |
| `execute_trade_plan` | plan_id, plan_hash, request_id | Revalida, reserva y envía; devuelve resultado o estado incierto persistido | executor, mutación de bróker |
| `get_execution_status` | request_id | Estado actual y evidencias/reconciliación; no reenvía | observer con alcance, lectura |
| `modify_trade_group` | grupo, expected_version, patch de protecciones, request_id | Plan/resultado verificado por pata; omisión conserva valor y eliminación es explícita | executor, mutación de bróker |
| `close_trade_group` | grupo, alcance/volumen o fracción permitida, expected_version, request_id | Cierre verificado y volumen restante por posición/grupo | executor, mutación de bróker |
| `cancel_trade_group_orders` | grupo, expected_version, request_id | Cancelación de pendientes propias y reconciliación de carreras con llenado | executor, mutación de bróker |

Los identificadores de grupo se resuelven en el servidor; no confiar en una lista de tickets arbitrarios del modelo. Un cierre reintenta solo el volumen restante verificado, dentro de su mandato. El bloqueo de nuevas entradas no bloquea automáticamente acciones protectoras autorizadas.

Las herramientas antiguas siguen disponibles para compatibilidad bajo el mismo gateway, autorización, journal y verificación. Añadir parámetros/versiones de forma explícita; no dejar los wrappers legacy como bypass de límites. Cambiar protecciones nunca debe borrar campos omitidos. La eliminación de SL requeriría permiso explícito y puede estar prohibida por la política demo.

### Gobierno, supervisor y evaluación

| Herramienta | Entradas principales | Salida/semántica | Capacidad |
|---|---|---|---|
| `configure_policy` | documento, expected_version, request_id | Nueva versión validada, inicialmente sin armar; no concede privilegios al propio autor | operator, mutación |
| `set_automation_state` | policy_id/version, observe/shadow/demo_execute, entries_enabled, mandato, request_id | Estado y concesión autorizada; no cambia límites implícitamente | operator o capacidad acotada, mutación |
| `configure_position_rule` | alcance, condición tipada, acción, expected_version, request_id | Regla durable y su estado, sin activación fuera del mandato | executor/operator con alcance, mutación |
| `get_automation_status` | policy_id/estrategia | Políticas, supervisores, colas, pausas, reservas, incidentes y latencias | observer, lectura |
| `get_audit_events` | filtros/cursor | Transiciones y procedencia con redacción por permisos | observer con permiso de auditoría, lectura |
| `get_strategy_performance` | estrategia/fuente/rango | Resultado realizado y flotante separados, costes, cobertura de datos, rechazos y atribución | observer, lectura |

Las reglas admiten condiciones tipadas/versionadas —umbral de P&L, breakeven, trailing, expiración, cierre de vela— y un pequeño lenguaje declarativo validado. No `eval`, scripts o prompts como expresión ejecutable. Aclarar cuándo una regla reduce riesgo y cuándo puede aumentarlo: mover SL también requiere verificación de precio/distancia y presupuesto.

Anotaciones propuestas: consultas `readOnlyHint=true`; mutaciones locales `readOnlyHint=false` y `openWorldHint=false` solo si efectivamente no salen del servicio; envío/gestión en bróker `readOnlyHint=false` y `openWorldHint=true`. `destructiveHint` se define por la operación real y los efectos, no se fuerza a false para reducir avisos. El guard de Rinari puede pedir autorización al cruzar orígenes; el ejecutor determinista con mandato previo permite un recorrido automático sin mentir sobre esos efectos.

El rol de gateway `observer` no equivale al perfil `read-only` de Rinari: el perfil instalado deniega MCP. Configurar perfiles/conectores compatibles y aplicar el alcance de lectura en el servidor, verificándolo con llamadas denegadas y permitidas durante E4.

## 4. Compatibilidad exacta con reglas de Agency

Los siguientes son **fragmentos propuestos del campo `tool`**, no archivos de configuración listos para importar ni reglas creadas. Los nombres de herramientas nuevas aún no existen. El propietario configuraría programación, agente, permisos y mandato en Agency.

Fragmento de lectura y análisis:

```json
{
  "server": "mt5-observer-example",
  "tool": "peek_signal_digest",
  "arguments": {
    "consumer_id": "agency-supervisor-example",
    "profile_id": "gold-demo-example",
    "limit": 10
  },
  "on_result": "agent"
}
```

Fragmento de evaluación sin órdenes:

```json
{
  "server": "mt5-analyst-example",
  "tool": "evaluate_signal_batch",
  "arguments": {
    "policy_id": "gold-demo-example",
    "mode": "shadow",
    "max_items": 5
  },
  "on_result": "notify"
}
```

El segundo fragmento necesita la excepción de ID de lote documentada arriba y autorización para mutaciones locales. Ensayar una herramienta de lectura no puede consumir señales. Para ejecutar demo se requiere otra capacidad y política armada; no cambiar `shadow` por ejecución en el texto de un mensaje.

Para un resultado sin novedades, `payload_text` de la versión instalada serializa cualquier `structuredContent` no vacío aunque exista un bloque textual vacío. Por ello usar un `CallToolResult` explícito sin JSON estructurado no vacío, conforme a la excepción del apartado 1. Probarlo con el transporte real: no basta devolver un diccionario `items: []` ni confiar en la serialización automática de FastMCP.

Acuse: después de analizar, el agente acusa los eventos con referencias a su decisión. Si el análisis falla, permanecen pendientes. Las decisiones y órdenes tienen dedupe propio aunque se repita el digest. El evaluador automático reclama/acusa internamente tras persistir su resultado; el digest de auditoría puede usar otro consumidor.

No usar `on_result=deliver` para pasar texto libre directamente a `place_market_order`. La entrega fija podría transportar una referencia de evento a un consumidor validador; el texto no debe convertirse en argumentos de orden sin esquema, política y preflight.

## 5. Errores mínimos

| Código | Tratamiento |
|---|---|
| `AUTHORIZATION_DENIED`, `ACCOUNT_MISMATCH`, `DEMO_REQUIRED` | Rechazar sin enviar; registrar incidente/redactar datos |
| `POLICY_INCOMPLETE`, `POLICY_PAUSED`, `POLICY_VERSION_CONFLICT` | No abrir; renovar configuración/mandato según caso |
| `SIGNAL_EXPIRED`, `SIGNAL_SUPERSEDED`, `CORRELATION_AMBIGUOUS` | Expirar, invalidar o cuarentena; no perseguir entrada |
| `SOURCE_NOT_ALLOWED`, `SYMBOL_NOT_MAPPED`, `UNTRUSTED_PROVENANCE` | Rechazar sin fallback de símbolo/fuente |
| `DATA_STALE`, `DATA_UNKNOWN`, `MARKET_NOT_TRADABLE` | Esperar/rechazar según TTL; nunca inventar estado |
| `RISK_LIMIT`, `VOLUME_NOT_FEASIBLE`, `INVALID_PROTECTION` | Rechazar o recalcular dentro de mandato; sin aumentar riesgo |
| `PLAN_EXPIRED`, `PLAN_CHANGED`, `STATE_VERSION_CONFLICT` | Repreparar/reconciliar; no ejecutar el plan anterior |
| `IDEMPOTENCY_CONFLICT`, `EXECUTION_IN_PROGRESS` | Consultar solicitud original; no otro envío |
| `BROKER_REJECTED`, `BROKER_OUTCOME_UNKNOWN` | Rechazo explícito o reconciliación con reservas conservadas |
| `PERSISTENCE_UNAVAILABLE`, `GATEWAY_OWNER_CONFLICT` | Bloquear escrituras; recuperar autoridad/persistencia |

## 6. Orden de construcción del catálogo

Primero salud/snapshot/specs/riesgo y planes/estado con journal; después fuentes/ingesta/parser/listado/digest/evaluador en shadow; luego candidatos/decisiones/ejecución demo y reglas de posiciones; finalmente incrementales, evaluación avanzada y controles de operación. Los nombres definitivos y esquemas completos deben congelarse en E0 del estudio, con tests de compatibilidad Agency y clientes MCP.

No es necesario ofrecer todo el catálogo a todos los agentes. Descubrimiento por capacidades y conectores limitados reduce errores y coste de contexto; el servidor sigue validando permisos al invocar. Mantener un registro de contrato único para documentación, modelos Pydantic/JSON Schema y pruebas, sin esquemas copiados que diverjan.
