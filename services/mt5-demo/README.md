# Despliegue demo para Rinari, núcleo 0.7.0

Adaptador Windows versionado: las 45 herramientas nativas, `demo_status` y cuatro herramientas de investigación (`get_symbol_specs`, `get_market_context`, `calculate_trade_risk`, `get_gold_research`). Total: **50**, sin sobrescribir el preview ni el snapshot nativos.

Usa el terminal ya abierto y exige cuenta DEMO, conexión y cuenta/servidor estables. Comprueba de nuevo permisos y cuenta justo antes de `order_send`. No crea órdenes ni reglas al iniciar. La investigación captura MT5 mediante el worker serializado; el análisis SSH se ejecuta fuera del worker para permitir consultas y gestión mientras espera la red.

## Instalación privada

En Windows, dentro del directorio MCP del propietario, crear un entorno separado `.venv-0.7`, instalar este repositorio y ejecutar `services/mt5-demo/demo_server.py`. `MT5_DEMO_HOME` permite elegir el directorio privado; por defecto es el padre del repositorio. El launcher estable `MCP/demo_server.py` debe ejecutar este módulo con el Python nuevo, heredando stdin/stdout/stderr. Conservar el entorno/launcher anterior para rollback. No ejecutar simultáneamente la versión antigua: no comparte el registro nuevo de solicitudes.

Configuración opcional `MCP/demo-config.json` (no subirla): `terminal_path`, `state_db`, `supervisor_enabled` y `duplicate_window_seconds` (3.0). El estado predeterminado es `MCP/data/state.sqlite3`; todas las conexiones deben usar ese mismo archivo. Mantener DB, logs, sesiones, credenciales y backups fuera de Git. Respaldar SQLite con su API de backup antes de migraciones posteriores. Los límites monetarios se configuran mediante las variables de riesgo documentadas por el núcleo; no se inventan importes.

`python services/mt5-demo/demo_server.py --check` conecta, imprime diagnóstico demo y termina sin enviar órdenes. STDIO es el único transporte de este despliegue. Logs rotativos privados: `MCP/logs/mcp-0.7.log`, stderr desactivado para evitar bloquear el transporte de Rinari.

## Migración del agente

| Operación | Contrato 0.7 |
|---|---|
| Estado de cuenta/cotización | `get_trading_snapshot(symbol="XAUUSDm")`; leer `error/message/data` y fallos de cada componente |
| Comprobar apertura | `preview_order(symbol="XAUUSDm", type="BUY" o "SELL", volume=… o risk_amount=…, stop_loss=…, take_profit=…)` |
| Enviar apertura autorizada | Usar `data.send_with.arguments`, incluido `preview_id`; añadir un `request_id` estable por intención |
| Timeout/resultado incierto | Consultar `get_request_status` y `reconcile_request`; no reenviar con otro identificador |
| Velas cerradas | `get_candles_multi(symbol="XAUUSDm", timeframes=["M1","M5","H4"], closed_only=True)` |
| Salud | `get_bridge_health`; el diagnóstico sin probe no espera al SDK de MT5 |
| Análisis MT5 → server2 | `get_gold_research()`; no cambiar timestamps para evitar `data_stale` |
| Gestión masiva | Scope explícito y primero `dry_run`; conservar propiedad por estrategia |
| Gestión local persistente | Reglas del supervisor solo bajo mandato explícito; revisar estado/eventos y límites |

La ventana de duplicados sigue en 3 segundos; no es la frecuencia del feed ni del supervisor. Las reservas duran 60 segundos. La coordinación persistente solo cubre procesos que usan la misma DB y cuenta, no otros robots/terminales externos.

## Fuentes y pendientes

WhatsApp Alejandrofx27 es la fuente autorizada habilitada; verificar heartbeat y eventos del receptor. El chat privado de pruebas de Telegram está deshabilitado. FXTradingVision tiene credenciales preparadas pero la sesión personal sigue pendiente de vinculación: no afirmar que ya llegan señales. `gold-research` consulta el digest; texto externo no autoriza operaciones.

La adaptación valida transporte, lectura real, análisis y preview sin envío. La ejecución 0.7 en terminal demo y las reglas con cierres reales requieren una prueba controlada posterior. Los importes máximos por operación/día y posiciones aún deben ser proporcionados por el propietario antes de armar apertura automática. Ver [flujo de Rinari](../gold-research/RINARI_WORKFLOW.md).
