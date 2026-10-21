# Plan del fork y revisión de MetaTrader MCP Server

Fork de trabajo: https://github.com/Xainner/metatrader-mcp-server

Este fork se mantiene para corregir los problemas detectados en la revisión antes de habilitar operaciones automáticas. La segunda entrega (versión 0.6.0) implementa los arreglos; la validación en una cuenta demo sigue pendiente.

## Pendientes conocidos tras fusionar el PR #1

La revisión de la versión 0.6.0 reprodujo tres problemas adicionales, resueltos en el commit `cd2001c`. Se detallan con ubicaciones, pasos de reproducción simulada, impacto y criterios de aceptación en [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).

- [x] **P1 / MT5-001:** rechazar aperturas si no se puede leer el estado necesario para comprobar exposición o pérdida diaria.
- [x] **P1 / MT5-002:** informar un cierre como no verificado si falla la consulta posterior de la posición.
- [x] **P2 / MT5-003:** reservar de forma atómica las solicitudes en curso y su exposición para clientes concurrentes.

Las casillas completadas del plan original describen la implementación base y sus pruebas existentes. Estos casos tienen sus propias pruebas de regresión en `tests/unit/test_known_issues.py`; como el resto, siguen pendientes de validación en una cuenta demo. El soporte SL/TP al abrir y la conservación de niveles al modificar pendientes sí están implementados en 0.6.0.

## Arreglos pendientes

- [x] Interpretar `retcode`, manejar resultados nulos y distinguir aceptación, ejecución parcial, ejecución completa y rechazo.
- [x] Preservar SL/TP al modificar únicamente el precio de una orden pendiente.
- [x] Exponer SL/TP en las órdenes de mercado del cliente y MCP; obtener el precio antes de validar sus niveles.
- [x] Usar un transporte local por defecto y proteger cualquier acceso remoto con autenticación.
- [x] Comprobar y comunicar cada cierre individual en las operaciones masivas.
- [x] Corregir la selección del modo de llenado según símbolo, tipo de ejecución y clase de orden.
- [x] Validar volumen mínimo, máximo y paso, precisión del precio y distancia mínima de los stops según el símbolo.
- [x] Incorporar límites configurables de riesgo, exposición y pérdida diaria, y prevención de solicitudes duplicadas.
- [x] Aislar las pruebas automáticas con simulaciones; exigir cuenta demo explícita para pruebas de integración que operen.
- [x] Verificar arranque, esquemas MCP y compatibilidad de dependencias; fijar versiones probadas.
- [ ] Validar los cambios en una cuenta demo dedicada antes de considerar uso real.

Las casillas solo deben marcarse tras implementar y verificar cada cambio. Las casillas marcadas se verificaron con pruebas automáticas contra un terminal simulado y con el arranque real del servidor sin credenciales; ninguna se verificó contra un servidor de trading real (ver "Implementación"). El alcance de la revisión inicial y las limitaciones de sus pruebas se describen a continuación.

---

## Implementación (versión 0.6.0)

Fecha: 20 de octubre de 2026.

| Ítem | Cambio | Verificación |
|---|---|---|
| Resultados | `order/_result.py` clasifica cada respuesta en `done`, `partial`, `placed`, `no_changes`, `rejected` o `no_response` y conserva `retcode`. Un `DONE` con volumen menor al pedido se trata como parcial. Se corrigieron los valores de `TradeReturnCodes`, que no coincidían con MetaTrader 5 (p. ej. `TIMEOUT` duplicaba 10010). | `tests/unit/test_orders.py`: rechazo 10019, `None`, parcial, colocada; enum contrastado con las constantes `TRADE_RETCODE_*` del paquete. |
| Reconciliación | La orden de mercado busca el deal en el historial y devuelve `position_id`; el cierre consulta la posición después y reporta el volumen restante. | `test_done_market_order_is_filled_and_reconciled`, `test_close_position_reports_remaining_volume`. |
| Modificar pendiente | Se leen la orden actual y se reenvían SL, TP, `type_time`, expiración y `stoplimit` no indicados. `0` elimina un nivel de forma explícita. | `test_modify_pending_price_keeps_sl_tp_and_expiration`, `test_modify_pending_zero_removes_level_explicitly`. |
| SL/TP en mercado | Parámetros en `MT5Order`, la herramienta MCP y la skill; la cotización se lee antes de validar y los stops se miden contra el precio de cierre (bid/ask). | Caso de la revisión (SL 1.09, ask 1.1001) envía la orden con SL y TP. |
| Transporte | STDIO por defecto; HTTP en `127.0.0.1`. Cualquier otra dirección exige `MCP_AUTH_TOKEN` (≥ 32 caracteres, Bearer) y TLS, salvo proxy declarado. La API REST aplica lo mismo (`OPENAPI_AUTH_TOKEN`), elimina el CORS `*` y `reload=True`. El servidor de cotizaciones escucha en `127.0.0.1`. | `tests/unit/test_transport.py`; servidor SSE real: 401 sin token o con token inválido, stream con token válido; arranque en `0.0.0.0` rechazado sin token y sin TLS. |
| Masivas | `order/_bulk.py` procesa cada ticket, informa su resultado y vuelve a consultar el terminal; `error` es verdadero si alguno falla o sigue presente. | `tests/unit/test_bulk.py`: dos cierres fallidos ya no devuelven éxito. |
| Llenado | Pendientes: RETURN. Request/Instant: FOK. Market: FOK o IOC según la máscara, error explícito si no hay ninguno. Exchange: FOK, IOC o RETURN. BOC nunca se usa en mercado. | Máscara 0 en Market devuelve error controlado en vez de `UnboundLocalError`; matriz de casos parametrizada. |
| Validación por símbolo | `order/_validation.py`: volumen mínimo, máximo y paso; precio según dígitos y tamaño de tick (se rechaza indicando el valor válido más cercano en vez de redondear); distancia `trade_stops_level` para SL, TP y precio de pendientes. | Pruebas parametrizadas en `test_orders.py`. |
| Riesgo | `metatrader_client/risk.py`: solo cuentas demo salvo `MT5_ALLOW_LIVE_TRADING=true`; límites opcionales de lotes por orden, exposición total, número de posiciones, % de riesgo en el SL, SL obligatorio y pérdida diaria (día del servidor, realizado + flotante); rechazo de solicitudes idénticas en 10 s, incluida la repetición tras un resultado desconocido. Los cierres no se bloquean por límites de exposición. | `tests/unit/test_risk.py`. |
| Pruebas | `tests/conftest.py` elimina credenciales y bloquea `initialize`, `login`, `order_send` y `order_check` en las pruebas unitarias. Las de `tests/metatrader_client` se marcan `integration`, solo corren con `MT5_RUN_INTEGRATION=1` y terminan la sesión si la cuenta conectada no es demo. Se corrigieron las pruebas de rutas REST, que estaban desactualizadas y fallaban. | `pytest`: 112 pruebas pasan, 39 de integración omitidas. |
| Dependencias | Rangos con límite superior de versión mayor (`mcp<2`, `pandas<3`, ...); `mcp>=1.25` es el mínimo comprobado; `uv.lock` regenerado; pytest y build pasan al extra `dev`. Se añadió el decorador que faltaba en `get_candles_by_date` y se corrigieron anotaciones de retorno de herramientas que devuelven CSV. | Arranque real por STDIO sin credenciales: `initialize` y `tools/list` responden con 26 herramientas. Suite en verde con `uv.lock` (mcp 1.30.0, pandas 2.2.3), con las últimas versiones compatibles (fastapi 0.156, pydantic 2.14) y con mcp 1.25. |

### Hallazgos adicionales corregidos

- `get_account_type` invertía demo y real: MetaTrader 5 define `ACCOUNT_TRADE_MODE_DEMO = 0`, `CONTEST = 1`, `REAL = 2`. Una cuenta real se informaba como demo.
- Con las dependencias sin límite superior, `pip install` resolvía `mcp 2.x` y el servidor no arrancaba (`ModuleNotFoundError: mcp.server.fastmcp`).
- La API REST permitía CORS desde cualquier origen sin autenticación: una página web abierta en el mismo equipo podía enviar órdenes a `127.0.0.1:8000`.
- El ciclo de vida del servidor MCP fallaba al cerrar sin credenciales (`None.disconnect()`).
- La skill de trading indicaba abrir sin SL y añadirlo después.

### Cambios de comportamiento

- El transporte por defecto pasa de SSE en `0.0.0.0` a STDIO. Quien use SSE debe indicarlo y configurar token y TLS para acceso remoto.
- Las operaciones se rechazan en cuentas no demo hasta habilitar `MT5_ALLOW_LIVE_TRADING`.
- Los resultados incluyen `status` y `retcode`; `data` sigue siendo el `TradeResult` en el cliente Python y un diccionario en MCP.
- Precios con más decimales que el símbolo se rechazan en lugar de enviarse.

### Pendiente: validación en cuenta demo

No se conectó ningún terminal ni se enviaron órdenes. Antes de considerar uso real:

1. Configurar `.env` con una cuenta demo dedicada y ejecutar `MT5_RUN_INTEGRATION=1 pytest tests/metatrader_client`.
2. Probar manualmente apertura con SL/TP, modificación de pendiente solo en precio, rechazo (volumen inválido o mercado cerrado), llenado parcial si el bróker lo permite, cierre individual y masivo.
3. Comprobar en la demo que el límite de pérdida diaria coincide con el día del servidor del bróker.
4. Revisar los límites de riesgo que se usarán y documentarlos junto al despliegue.

---

## Revisión inicial

Fecha: 20 de octubre de 2026.
Repositorio: https://github.com/ariadng/metatrader-mcp-server
Commit revisado: `bd2ff591e32cb492fafefe74221d502dd78b71ef`.
Versión declarada: 0.5.1.

## Dictamen

El proyecto implementa un puente real entre MCP y la API oficial de MetaTrader 5, pero esta revisión encuentra defectos que impiden recomendarlo tal como está para operar dinero real de forma autónoma. Puede servir como base después de corregir la ejecución, proteger el acceso y verificarlo en demo.

## Hallazgos

1. **Alta: operaciones rechazadas se anuncian como exitosas.** En `src/metatrader_client/order/send_order.py:197–204` y ramas equivalentes, se consulta `last_error()` sin interpretar `response.retcode`. Una respuesta simulada del bróker con `retcode=10019` (fondos insuficientes) produjo `error=false` y un mensaje de compra exitosa con ticket 0. Se debe distinguir rechazo, aceptación, ejecución completa y ejecución parcial, conservar los códigos y reconciliar el estado con el terminal. Referencia oficial: https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py

2. **Alta: modificar una orden pendiente puede eliminar sus protecciones.** `modify_pending_order.py` omite los parámetros no proporcionados, pero `send_order.py:295–296` los reemplaza por sus valores predeterminados 0.0. La simulación de cambiar únicamente el precio envió también `sl=0.0` y `tp=0.0`. Si el servidor acepta la solicitud, esos valores solicitan retirar las protecciones. Deben preservarse los valores actuales salvo eliminación explícita.

3. **Alta: apertura de mercado sin SL/TP por MCP.** `src/metatrader_mcp/server.py:149` y `src/metatrader_client/client_order.py:60` solo aceptan símbolo, volumen y dirección. La función interna sí tiene parámetros SL/TP, pero no se exponen por esas capas. Además, al llamar directamente a esa función, un BUY a mercado con SL válido se rechaza porque `send_order.py:140` compara el SL con el precio predeterminado 0 antes de obtener la cotización. Se reprodujo con SL 1.09 y ask simulado 1.1001; no llegó a enviar la solicitud.

4. **Alta: servidor MCP accesible por red sin autenticación configurada.** `src/metatrader_mcp/utils.py:8–24` elige SSE y `0.0.0.0` por defecto y desactiva la protección frente a DNS rebinding para esa dirección. La creación de FastMCP no configura autenticación. Un cliente con acceso de red al servicio podría acceder a herramientas de trading; la accesibilidad efectiva depende del firewall y la red. Para uso local conviene STDIO. Cualquier exposición remota requiere autenticación y transporte protegido.

5. **Alta: cierre masivo con éxito falso.** `src/metatrader_client/order/close_all_positions.py` ignora el resultado de cada cierre y aumenta el contador igualmente. Al simular dos cierres fallidos, devolvió `Close 2 positions success`. Debe informar cada resultado y comprobar las posiciones restantes.

6. **Media: selección incorrecta del modo de llenado.** `send_order.py:106–115` asocia el bit 4 con RETURN, aunque ese bit corresponde a BOC, y no establece alternativa si la máscara vale 0. Una simulación con máscara 0 produjo `UnboundLocalError` antes de enviar la orden. La elección también debe considerar el tipo de ejecución y si se trata de una orden pendiente. Referencia: https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants#symbol_filling_mode

7. **Controles insuficientes para autonomía.** El envío solo aplica un límite genérico de volumen de hasta 100 lotes. No encontré controles efectivos de pérdida diaria, riesgo por operación, exposición acumulada, repetición de solicitudes ni un bloqueo de cuentas reales en el recorrido de ejecución revisado. Estos límites deben imponerse por código fuera del criterio del agente.

## Validación realizada

- Lectura del código de conexión, herramientas MCP, envío y gestión de órdenes, interfaces HTTP y pruebas.
- Ejecución aislada de funciones extraídas del código original, con API MT5 y datos simulados. Se verificaron los seis casos descritos como reproducidos o comprobados por firma; no se importó la API real ni se inició sesión.
- Resolución de dependencias con `uv pip install --dry-run` en un entorno virtual Python 3.12: completada. No demuestra que el servidor arranque; las dependencias tienen mínimos sin límites superiores y hoy resuelven, entre otras, MCP 2.3.0 y pandas 3.0.6.
- Confirmado el proceso `terminal64.exe` y su ejecutable en `C:\Program Files\MetaTrader 5\terminal64.exe`.
- Disponibles Python 3.10, 3.11, 3.12 y 3.14; se utilizó 3.12 para comprobar resolución.

No se instaló ni arrancó el servidor, no se leyó la cuenta y no se enviaron órdenes. No se ejecutó la suite original porque contiene pruebas que abren operaciones y cierran posiciones usando credenciales del entorno, sin imponer que la cuenta sea demo. Esta revisión no certifica las dependencias ni el paquete publicado en PyPI: examina el commit indicado.

## Trabajo necesario antes de operar

Corregir los resultados y las modificaciones de órdenes, permitir SL/TP en la apertura, elegir correctamente el modo de llenado y comprobar el volumen permitido por cada símbolo. Después, configurar acceso local, limitar riesgo y exposición, evitar duplicados y probar apertura, modificación, rechazo, llenado parcial y cierre en una cuenta demo dedicada. Tener MT5 instalado cumple un requisito, pero no valida la ejecución del MCP.
