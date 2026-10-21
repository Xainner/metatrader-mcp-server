# Pendientes conocidos de la versión 0.6.0

Actualizado: 20 de octubre de 2026.

Estado: **tres hallazgos resueltos en código** (commit `cd2001c`), pendientes de validación en una cuenta demo dedicada junto con el resto de la ejecución.

Referencia: [PR #1](https://github.com/Xainner/metatrader-mcp-server/pull/1), fusionado en `5ff4d32ce411ba4ade02432b649619a16da57aeb`. La revisión y las reproducciones corresponden a su contenido, cuyo commit de origen es `d07164159ff81f26ef79e1a10cd8946dd302eab7`.

La versión 0.6.0 ya permite SL/TP en la apertura de mercado y conserva los niveles omitidos al modificar una pendiente. Los pendientes de este documento afectan a la evaluación del riesgo, la confirmación de cierres y el uso concurrente de la biblioteca.

| ID | Prioridad | Estado | Problema |
|---|---|---|---|
| MT5-001 | P1 | Resuelto (`cd2001c`) | Una lectura fallida del estado se trata como exposición o pérdida flotante cero. |
| MT5-002 | P1 | Resuelto (`cd2001c`) | La consulta final de un cierre puede fallar y aun así devolverse cierre exitoso. |
| MT5-003 | P2 | Resuelto (`cd2001c`) | Solicitudes concurrentes pueden superar la prevención de duplicados y los límites de exposición. |

P1 indica un problema que debe resolverse antes de confiar en el control o la confirmación afectados. P2 indica un problema que debe corregirse antes de admitir el escenario concurrente descrito. Estas prioridades son de la revisión de este fork.

## MT5-001 — Bloquear nuevas aperturas cuando el riesgo no pueda comprobarse

**Ubicación:** [risk.py](../src/metatrader_client/risk.py), `RiskGuard.check_new_order` (líneas 153–154 del commit revisado) y `RiskGuard.check_daily_loss` (línea 243).

### Comportamiento actual e impacto

La expresión `mt5.positions_get() or ()` convierte tanto una respuesta vacía válida como `None` por error en una colección vacía. Ocurre lo mismo con `orders_get()` en el control de exposición.

Si MT5 no puede devolver posiciones u órdenes, el código puede autorizar nuevas aperturas como si no existiera exposición previa. No puede garantizarse el cumplimiento de `max_open_positions` ni `max_total_volume` con ese estado desconocido. En pérdida diaria, un error al leer posiciones también elimina del cálculo la posible pérdida flotante y puede permitir otra orden cuando debería bloquearse.

### Reproducción con simulación

1. Crear `FakeMT5` de `tests/unit/fakes.py` y un `RiskGuard` con `max_total_volume=0.05` y `max_open_positions=1`.
2. Sustituir `positions_get` y `orders_get` del simulador por funciones que devuelvan `None`.
3. Invocar `check_new_order` para un BUY de 0.01 lotes, con precio 1.1001 y los demás límites desactivados.
4. Resultado observado: devuelve `None` como error; la apertura queda autorizada pese a no poder evaluar la exposición.

El límite y el volumen son datos de una prueba de software, no parámetros recomendados para operar. El mismo patrón de lectura requiere cobertura en `check_daily_loss` con el historial disponible y las posiciones ilegibles.

### Corrección propuesta

Leer cada listado una vez y distinguir explícitamente `None` de una colección vacía. Cuando un límite habilitado necesite un dato ilegible, devolver un error y bloquear nuevas aperturas antes de `order_send`. Conservar el contexto de la consulta y `last_error()` para diagnóstico. Mantener disponibles las operaciones de reducción/cierre según su recorrido de validación; una pérdida de lectura no debe causar un reintento automático de apertura.

### Criterios de aceptación

- [x] Con límite de exposición habilitado, fallo de `positions_get` bloquea la apertura sin enviar órdenes.
- [x] Fallo independiente de `orders_get` produce el mismo bloqueo.
- [x] Tuplas vacías válidas permiten continuar cuando se cumplen los límites.
- [x] Con pérdida diaria habilitada, posiciones ilegibles bloquean nuevas aperturas aunque el historial sea legible.
- [x] Las pruebas verifican los errores devueltos y que el simulador no registra envíos.

### Resolución

Commit `cd2001c398f28a54804a24d6d08ebbe49c400413`. `RiskGuard` lee cada listado una vez mediante `_read`, que distingue `None` de una tupla vacía e incluye `last_error()` en el mensaje. Si `max_open_positions`, `max_total_volume` o `max_daily_loss` necesitan un dato ilegible, la apertura se rechaza antes de `order_send` con «Cannot verify … new orders are blocked». Los límites desactivados no consultan los listados, y los cierres no pasan por estas comprobaciones. Pruebas: `tests/unit/test_known_issues.py::test_mt5_001_*`.

## MT5-002 — Distinguir cierre confirmado de comprobación fallida

**Ubicación:** [close_position.py](../src/metatrader_client/order/close_position.py), `close_position`, líneas 55–56 del commit revisado.

### Comportamiento actual e impacto

Después de una respuesta de trading considerada exitosa, se consulta de nuevo el ticket. El código solo comprueba `if remaining`. Tanto `()` como `None` hacen que se conserve `error=false` y el mensaje `Closed position ...`.

Una consulta fallida no demuestra que la posición haya desaparecido. El agente puede continuar creyendo que cerró toda la exposición aunque el estado final siga sin verificarse. Una respuesta `placed` o `partial` tampoco equivale por sí sola a cierre completo.

### Reproducción con simulación

1. Configurar un `FakeMT5` con una posición de ticket 42 y una respuesta de envío `DONE`.
2. Hacer que la primera llamada a `positions_get(ticket=42)` devuelva la posición y la consulta posterior devuelva `None`.
3. Llamar a `close_position(connection, 42)` usando exclusivamente el simulador.
4. Resultado observado: `error=false` y `Closed position 42 at price 1.1 (DONE)`.

### Corrección propuesta

Tratar `remaining is None` como fallo de verificación. Devolver una indicación explícita de estado final desconocido y conservar el `retcode`, resultado y ticket de la solicitud enviada. El detalle de verificación debe distinguirse del resultado de envío: no afirmar que el bróker rechazó una orden que pudo haber ejecutado. Pedir reconciliación antes de cualquier repetición. Solo afirmar cierre completo cuando una consulta válida no encuentre volumen restante.

### Criterios de aceptación

- [x] Consulta final `None`: respuesta no exitosa o explícitamente no confirmada; nunca mensaje de cierre completo.
- [x] Consulta final `()`: cierre confirmado si la solicitud y el estado lo justifican.
- [x] Posición restante: informar volumen pendiente y distinguir ejecución parcial.
- [x] Conservar resultado, ticket y `retcode` para reconciliar el estado.
- [x] Los cierres masivos conservan ese estado por ticket y no cuentan un cierre sin verificar como confirmado.
- [x] Las pruebas no repiten automáticamente la solicitud ante un resultado incierto.

### Resolución

Commit `cd2001c398f28a54804a24d6d08ebbe49c400413`. `close_position` devuelve `verification`: `confirmed` solo si una consulta válida ya no lista la posición; `remaining` con `remaining_volume` si sigue abierta; `unknown` si la consulta final devuelve `None` o el envío no tuvo respuesta; `not_closed` si fue rechazado. Solo `confirmed` devuelve `error: false`. Se conservan `status`, `retcode`, `data` y `position_id`, y el mensaje pide revisar las posiciones antes de repetir. Las operaciones masivas listan esos tickets en `unverified`, no los cuentan en `succeeded` y solo los confirman si su consulta final es válida y ya no los incluye. Pruebas: `test_mt5_002_*`.

## MT5-003 — Reservar solicitudes y exposición antes de enviarlas

**Ubicación:** [send_order.py](../src/metatrader_client/order/send_order.py), comprobación/envío de aperturas y `_send`; [risk.py](../src/metatrader_client/risk.py), `check_duplicate` y `record`.

### Comportamiento actual e impacto

`check_duplicate()` toma y libera su bloqueo; `record()` vuelve a tomarlo después del envío. Dos hilos que compartan conexión y guard pueden comprobar la misma clave antes de que cualquiera la registre. También pueden aprobar el mismo presupuesto de exposición antes de que la primera orden se refleje en el terminal.

La ventana anti-duplicados y `max_open_positions` no garantizan el límite en este escenario. Tener un bloqueo dentro de cada método no protege toda la secuencia de comprobación, reserva y envío.

### Reproducción con simulación

1. Crear un `FakeMT5` y una conexión que comparta `RiskGuard(RiskConfig(max_open_positions=1, duplicate_window_seconds=10))`.
2. Ejecutar dos llamadas idénticas a `send_order` desde un `ThreadPoolExecutor` de dos hilos: BUY EURUSD, 0.1 lotes.
3. Introducir una barrera de dos participantes al entrar en el `order_send` simulado para que ambos hilos hayan pasado las comprobaciones antes de completar el primer envío.
4. Hacer que cada envío simulado devuelva éxito y añada una posición a su estado en memoria.
5. Resultado observado: **dos solicitudes enviadas y dos posiciones**, pese al máximo de una posición y la ventana de 10 segundos.

La reproducción corresponde a la biblioteca usada concurrentemente. No demuestra por sí misma concurrencia de herramientas en una sesión STDIO determinada.

### Corrección propuesta

Reservar de forma atómica la clave de la solicitud y el presupuesto de exposición antes de enviarla. Cubrir también órdenes distintas que compitan por el último espacio o volumen permitido. Definir la liberación de reservas para rechazo conocido y la conservación/reconciliación cuando el resultado sea incierto. Una solución que serialice el proceso completo puede ser suficiente para el alcance de una conexión, pero debe declarar sus límites entre conexiones y procesos.

### Criterios de aceptación

- [x] Dos solicitudes idénticas simultáneas con ventana activa generan como máximo un envío.
- [x] Dos órdenes distintas que compitan por un único espacio o presupuesto no pueden superar el límite entre ambas.
- [x] Rechazo conocido libera la reserva conforme a una política explícita y comprobada.
- [x] `None`, timeout u otro resultado incierto no permiten duplicar inmediatamente una solicitud que pudo ejecutarse.
- [x] Un llenado parcial actualiza correctamente la reserva y la exposición pendiente.
- [x] Pruebas concurrentes usan sincronización determinista, sin depender de pausas arbitrarias.
- [x] Documentar el alcance de la protección: una conexión, varias conexiones o procesos; no prometer coordinación que no exista.

### Mitigación de despliegue y límites

El adaptador local de Rinari instalado durante la revisión serializa las llamadas a herramientas mediante un bloqueo por proceso. Mitiga solicitudes simultáneas dentro de ese proceso; no coordina otros procesos, el terminal manual ni otros robots. Ese adaptador es externo al código distribuido de este repositorio y no cierra MT5-003.

### Resolución

Commits `cd2001c398f28a54804a24d6d08ebbe49c400413`, `546517caef10f4723eea7067d1d195722f1be27c` y `19137d73cb40f288cc157f4c846b19092ca1f144` (seguimientos de las revisiones del PR #2). `send_order` ejecuta la comprobación de duplicados, los límites, `order_send` y la reserva dentro de `RiskGuard.opening()`, un bloqueo por guard (uno por conexión).

Cada reserva registra el volumen comprometido (ejecutado + activo) y se reconcilia **por volumen** hasta que el terminal lo lista o vencen `RESERVATION_TTL_SECONDS` (60 s): sigue reservado el comprometido menos lo que los listados muestran para su ticket, es decir, el volumen de las posiciones cuyo `ticket` o `identifier` coincide más el `volume_current` de la orden residual. La mera presencia del ticket no libera nada, de modo que un listado que va por detrás de otro no oculta exposición. Una reducción que las posiciones no explican (remanente llenado aún sin publicar, o cancelado) se conserva hasta el TTL: es la política explícita para estado incierto.

La posición esperada para la parte ejecutada y la orden esperada para la parte activa ocupan un hueco de `max_open_positions` cada una mientras no aparezcan, igual que harán en el terminal. Política (`RiskGuard.after_send`):

- `done`: el volumen solicitado como ejecutado.
- `partial`: el volumen llenado como ejecutado. El remanente queda como activo con RETURN o con una política desconocida; con IOC o FOK lo cancela el servidor y no se reserva.
- `placed`: el volumen solicitado como activo (pendientes y órdenes de mercado aceptadas sin llenar).
- `no_response`: el volumen solicitado durante todo el TTL; la clave queda registrada como duplicado.
- Rechazo conocido: no se reserva nada.

Alcance: hilos que comparten una conexión en un proceso. No coordina otros procesos, otras conexiones, la operativa manual ni otros robots sobre la misma cuenta. En cuentas *netting*, la orden que amplía una posición existente no coincide con un ticket nuevo y sigue contando hasta que vence el TTL (criterio conservador). Si el bróker publicara el remanente RETURN con otro ticket, también seguiría reservado hasta el TTL. Pruebas: `test_mt5_003_*`, sincronizadas con eventos y sin pausas.

### Seguimiento de la revisión del PR #2

La revisión del commit `9fc7a75` encontró que un `partial` reducía la reserva al volumen ejecutado aunque, con `ORDER_FILLING_RETURN`, el remanente sigue activo ([MQL5](https://www.mql5.com/en/book/automation/experts/experts_execution_filling)). Reproducción: Exchange con máscara 0 (RETURN), `max_total_volume=0.15`, BUY 0.10 llenado 0.04 y listados vacíos: se autorizaba otra orden de 0.10 (0.20 en total). Resuelto en `{short}`; pruebas en `tests/unit/test_known_issues.py` (sección «MT5-003 follow-up»):

- [x] RETURN parcial con publicación demorada: el volumen ejecutado más el remanente impide superar el límite con una segunda orden (`test_review_reproduction_return_partial_with_delayed_listing`).
- [x] IOC parcial: la reserva corresponde al ejecutado cuando el resto fue cancelado (`test_ioc_partial_reserves_only_the_executed_volume`).
- [x] Orden residual visible y posición aún no visible: no se pierde el presupuesto de la parte ejecutada (`test_working_remainder_listed_without_position_keeps_executed_budget`).
- [x] Posición y remanente visibles: se reconcilian las reservas sin contar dos veces el mismo volumen (`test_position_and_remainder_listed_are_not_counted_twice`).

Segunda revisión (commit `1331b0d`): la orden residual visible liberaba todo el remanente aunque listara menos volumen. Con 0.04 ejecutados + 0.06 activos, otro llenado de 0.04 publicado primero en la orden (0.02 activos) y no en la posición (0.04) permitía otra orden de 0.09 (0.19 en total, límite 0.15). Resuelto en `19137d7` reconciliando por volumen:

- [x] Posición 0.04 + remanente 0.02 publicados: una nueva orden de 0.09 se bloquea (`test_second_review_reproduction_lagging_position_listing`).
- [x] Posición 0.08 + remanente 0.02 publicados: se reconcilia sin doble conteo y siguen contando 0.10 (`test_published_fills_are_reconciled_without_double_counting`).
- [x] Reducción del remanente sin evidencia de ejecución (posible cancelación): se conserva hasta el TTL (`test_unexplained_remainder_reduction_stays_reserved_until_ttl`).

Contra `1331b0d` falla la reproducción; las otras dos pruebas comprueban comportamiento que ya era correcto.

Casos adicionales cubiertos: posición visible con el remanente aún no listado, remanente llenado después dentro de la posición, política de llenado desconocida (se conserva el remanente) y los dos huecos que ocupa un RETURN parcial. Contra el código de `9fc7a75` fallan 5 de estas 8 pruebas; las otras 3 comprueban comportamiento que ya era correcto.

## Validación realizada y trabajo restante

- La suite sin integración de 0.6.0 pasó: **112 pruebas aprobadas, 39 de integración excluidas**, con una advertencia de deprecación de Starlette/httpx.
- Los tres hallazgos se reprodujeron con `FakeMT5` y respuestas en memoria; no se enviaron operaciones.
- Tras `19137d7`: 146 pruebas aprobadas y 39 de integración excluidas.
- Tras `546517c`: 143 pruebas aprobadas y 39 de integración excluidas.
- Tras `cd2001c`: 135 pruebas aprobadas y 39 de integración excluidas. Las 23 pruebas de regresión se ejecutaron también contra el código anterior: 19 fallan allí (las 4 restantes comprueban comportamiento que no debía cambiar).
- En el despliegue local se verificaron conexión demo, esquema SL/TP, consultas de lectura de XAUUSDm y permisos de trading. `ready_to_trade: true` describe permisos, no una certificación de ejecución o de los controles de riesgo.
- Sigue pendiente validar en una cuenta demo dedicada apertura con SL/TP, modificación, rechazo, ejecución parcial cuando sea posible y cierres. Las pruebas de integración pueden abrir y cerrar operaciones; deben ejecutarse deliberadamente en una cuenta dedicada.

Para cerrar cada hallazgo: implementar la corrección, añadir sus pruebas de regresión, ejecutar la suite pertinente y actualizar su estado en este documento y en [FORK_PLAN.md](../FORK_PLAN.md), citando el commit que lo resuelve.

Si un adaptador o agente usa `upstream_fixes_pending`, debe relacionar el aviso con los pendientes reales y mantenerlo actualizado. En 0.6.0 ese aviso no debe afirmar que falta soporte SL/TP en la apertura ni que omitir un nivel al modificar una pendiente lo elimina: esos problemas iniciales ya fueron corregidos.
