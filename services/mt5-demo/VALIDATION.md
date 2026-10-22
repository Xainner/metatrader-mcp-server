# Validación del despliegue 0.7 — 2026-10-22

Base instalada: `207496cb29111720c0eefaf613982a5febac0863` (PR #3). Adaptador y guía versionados en esta entrega. Python 3.12, MCP SDK 1.30.0, MetaTrader5 5.0.6231. Entorno anterior 0.6 conservado con backup del launcher y registro Rinari.

- `python -m pytest -q tests services/mt5-demo/tests services/gold-research/tests/test_windows_tools.py`: **269 passed, 39 skipped**. Las omitidas requieren integración con operaciones demo. Aviso de deprecación Starlette/httpx sin fallos.
- Pruebas del adaptador: rechazar cuenta real/cambiada/API deshabilitada antes del envío (SDK simulado); Context no expuesto en esquemas; preview/snapshot nativos conservados; análisis de red no ocupa el worker MT5.
- Handshake STDIO antes y después de desplegar: **50 herramientas**, sin duplicados; demo 0.7.0, permisos activos, ventana de duplicados 3 segundos.
- Snapshot real XAUUSDm y velas cerradas M1/M5/H4 correctos. Investigación con MT5 → server2: `status=ok`, 159 velas cerradas en cada M1/M5/M15/H1/H4.
- Preview nativo con SL/TP: `ready=true`, `order_check.retcode=0`; no se llamó a herramienta de envío. Investigación + preview aproximadamente 0.8 s en estas muestras; no es garantía de latencia ni llenado.
- Salud: puente sin bloqueo, reconexión habilitada; supervisor iniciado sin reglas configuradas. Se verificó lista vacía, no se crearon reglas.
- Registro Rinari actualizado conservando su ID y permisos: Python `.venv-0.7`, launcher estable; timeout de cliente 75 s para permitir el deadline nativo de envío de 60 s.

**Órdenes enviadas por esta actualización: 0.** No prueba llenado, cierre ni reglas de supervisión con órdenes reales del terminal. Importes de política y vinculación de Telegram personal siguen pendientes; ver README.
