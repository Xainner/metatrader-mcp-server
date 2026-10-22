# Integración operativa de Telegram, forex-gold, Rinari y MT5

Fecha: 21 de octubre de 2026 (America/Costa_Rica).

## Recorrido instalado

`Telegram autorizado → Signal Hub → Gold Research → Rinari → metatrader-demo → MT5`.

Forex-gold se utiliza como biblioteca de parsing y análisis en revisión `83d7faf8d7e131a9ed353c7920a2c456cc088807`. No se inicia su aplicación, ejecutor, bot de comandos ni administración remota. Las órdenes mantienen la vía MCP demo existente.

## Componentes disponibles

| Conector registrado en Rinari | Herramientas verificadas | Función |
|---|---|---|
| `trading-signals` | 3 | Recepción e historial de mensajes/ediciones de los chats permitidos |
| `gold-research` | 7 | Parsing, validación, búsqueda en señales recibidas, revisiones, análisis, digest y acuse |
| `metatrader-demo` | 33 | Herramientas MT5 anteriores más snapshot, specs, contexto, investigación, cálculo monetario y preflight |

Los dos servicios Linux son `trading-signal-hub` y `gold-research`, ambos systemd de usuario bajo `~/trading/`. El segundo reutiliza el historial del primero: no hay dos consumidores de Telegram. Los conectores remotos usan SSH y no contienen el token.

La integración Windows se carga desde `services/gold-research/client/windows_tools.py` en el adaptador privado `demo_server.py`, con copia previa de ese adaptador. No cambia las funciones de apertura/cierre originales. El análisis remoto se realiza fuera del lock de MT5 para no mantenerlo ocupado durante SSH.

## Verificación realizada

- 13 pruebas locales aprobadas, incluyendo controles positivos/negativos de patrones, parsing estricto, mensajes de gestión, velas cerradas, datos viejos, recibos por consumidor, digest vacío y simulación de cálculo/preflight Windows.
- En Linux se ejecutan las pruebas de investigación y transporte; las tres específicas del adaptador Windows se excluyen por plataforma y se prueban en Windows.
- Lectura real de MT5 en demo, con 159 barras cerradas por M1/M5/M15/H1/H4 a partir de una captura de 160 barras por marco.
- Recorrido real Windows → SSH → análisis en server2 → respuesta MCP: 0,765 segundos en una consulta. No es un benchmark ni una garantía de latencia.
- Esa consulta produjo cero candidatos, un resultado válido de los filtros; no se generó una señal ficticia para mostrar actividad.
- Cálculo real de pérdida al SL y margen en USD mediante MT5. `order_check` real respondió retcode 0 para una solicitud de prueba que **no se envió** al bróker.
- Conexión y descubrimiento verificados con el CLI instalado de Rinari en los tres conectores. No se enviaron órdenes ni mensajes Telegram durante las pruebas.

## Cómo usarlo

Pedir a Rinari: “Consulta `gold-research` y el estado demo de MT5. Revisa señales nuevas y el análisis de `get_gold_research` para XAUUSDm. Explica si hay un candidato o por qué no lo hay; no abras una operación sin un mandato de riesgo completo”.

La [guía de trabajo de los agentes](../services/gold-research/RINARI_WORKFLOW.md) define propuesta, validación, decisión, preflight, ejecución por MCP y verificación. Si una conversación anterior conserva el catálogo antiguo, reconectar ese conector o iniciar una conversación que descubra las herramientas actualizadas; no reiniciar el terminal MT5 para refrescar el catálogo.

`peek_signal_digest` es de lectura y devuelve texto vacío si no hay novedades. El agente registra su decisión y luego acusa únicamente las secuencias procesadas mediante `ack_signal_events`. Eso evita repetición sin confundir recepción, evaluación y ejecución.

## Límites explícitos

- La búsqueda cubre señales recibidas de fuentes autorizadas. No descubre canales privados de Internet ni aporta acceso a proveedores no configurados.
- Los candidatos técnicos no son una autorización para operar ni una probabilidad de ganancia. Se utilizan parámetros publicados del upstream, sin calibración de cuenta ni modelo ML entrenado en esta instalación.
- No se ha configurado feed de noticias ni se declaran noticias verificadas. No se levantó la aplicación/dashboard completa de forex-gold.
- La extracción no corrige ni inventa TP. Mensajes de gestión y respuestas requieren correlación con la operación real; “CLOSE ALL” no autoriza cerrar la cuenta.
- Las nuevas herramientas de cálculo/preflight no sustituyen los límites del ejecutor. Las mejoras generales de journal persistente y riesgo del [estudio](AUTOMATION_IMPLEMENTATION_STUDY.md) conservan su propio alcance; no se dan por implementadas por añadir este servicio de investigación.
- No se crearon tarjetas/reglas de Agency ni se armó apertura automática. Se confirmó la moneda USD, pero faltan importes de riesgo por operación, pérdida diaria y límite de posiciones para definir esa política. El MCP demo existente conserva su capacidad de operar bajo instrucciones autorizadas.

Código y detalles del componente: [Gold Research](../services/gold-research/README.md). El componente declara AGPL-3.0-only por separado; no altera las licencias originales del resto del fork.
