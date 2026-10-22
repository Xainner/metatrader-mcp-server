# Gold Research MCP — Forex-gold + Telegram + Rinari

Componente funcional de investigación. Forex-gold aporta sus parsers y funciones de análisis; `metatrader-demo` conserva las órdenes, SL/TP, modificaciones y cierres en MT5. Esta integración **no arranca la aplicación de forex-gold** ni sus motores de ejecución, administración remota, bot de control o proveedores LLM.

## Datos y herramientas

En `server2`, instalar este paquete y un checkout sin modificar de `MooreSi/forex-gold` en la revisión `83d7faf8d7e131a9ed353c7920a2c456cc088807`. El adaptador comprueba revisión y cambios de archivos rastreados antes de importar funciones.

El procesador lee los eventos reales del Signal Hub, conserva el origen/revisión, extrae señales y registra validaciones. Los precios omitidos no se inventan. La corrección/extrapolación automática de TP del parser upstream se deshabilita en el entorno aislado de la función; el checkout original no se modifica. Mensajes de gestión y cambios sin identidad de operación requieren correlación; no ejecutan cierres generales.

Herramientas `gold-research`:

- `get_research_status`: capacidades y estado real del procesado.
- `parse_gold_signal`: parsing y validación de entradas/SL/TP explícitos.
- `analyze_gold_market`: analiza OHLC/ticks aportados por el llamante, con procedencia declarada.
- `search_gold_signals`: busca en las señales recibidas de fuentes autorizadas, no descubre canales privados de Internet.
- `get_signal`: historial y revisiones de un mensaje/señal.
- `peek_signal_digest`: novedades por consumidor; contenido realmente vacío cuando no hay trabajo para Agency.
- `ack_signal_events`: acuse local explícito por evento y consumidor; no confirma una orden del bróker.

Las seis primeras son de lectura. El acuse escribe únicamente en el registro de investigación. SQLite de eventos y recibos mantiene deduplicación/reinicio. Un consumidor no consume el trabajo de otro.

En el MCP demo Windows se registran seis herramientas adicionales mediante `client/windows_tools.py`:

- `get_symbol_specs`: especificaciones del XAUUSDm de tu bróker.
- `get_trading_snapshot`: cuenta, precio, posiciones y pendientes, con errores explícitos.
- `get_market_context`: OHLC reales de M1, M5, M15, H1 y H4.
- `get_gold_research`: captura esos datos y solicita análisis a server2 mediante SSH, fuera del lock de MT5.
- `calculate_trade_risk`: pérdida estimada al SL y margen en moneda de cuenta; volumen se redondea hacia abajo.
- `preview_order`: validación y `order_check`, sin enviar órdenes.

Análisis disponible: sesgo H1/H4, ATR, ADX, MACD, régimen, niveles clave y candidatos break-and-go, break-and-retest y liquidity sweep. Son cálculos y candidatos de investigación, no probabilidad de acierto ni ventaja demostrada. Se usan parámetros publicados del upstream, sin calibración de tu cuenta. No se declara un modelo ML entrenado ni un feed de noticias configurado.

Las velas se ordenan/validan, se comprueba el reloj del bróker con el tick y se usan únicamente barras cerradas. Datos viejos, insuficientes o con reloj incierto producen estado explícito sin candidatos. El análisis no manda órdenes ni evita que MT5 revalide el precio al ejecutar.

## Instalación en server2

Runtime privado `~/trading/gold-research/`; configuración `config.json`:

```json
{
  "upstream_path": "/home/xainner/trading/repos/forex-gold",
  "raw_database": "/home/xainner/trading/signal-hub/data/signals.sqlite3",
  "source_id": "telegram-main"
}
```

Crear venv e instalar solo este componente con `pip install -e ~/trading/repos/metatrader-mcp-server/services/gold-research`. No hace falta instalar los paquetes de trading/ML de la aplicación completa para estas funciones puras. Inicializar con `python -m gold_research init`; probar con `process-once`; instalar la unidad de usuario `deploy/gold-research.service`. No copiar el token del receptor: se reutiliza el historial local, sin segundo consumidor de Telegram.

MCP por SSH:

```text
ssh -T -o BatchMode=yes -o ConnectTimeout=10 server2 /home/xainner/trading/gold-research/.venv/bin/python -m gold_research mcp
```

Procesador: `systemctl --user status gold-research`. La ejecución de una regla de Agency requiere que el propietario asigne agente, conectores y permisos adecuados. Se proporciona la guía `RINARI_WORKFLOW.md`; no se crean reglas ni se asignan límites monetarios implícitos al instalar.

## Pruebas

Configurar `GOLD_FOREX_REPO` con el checkout revisado y ejecutar `python -m unittest discover -s tests -v` en este paquete. Las pruebas usan datos sintéticos y no operan el bróker. La prueba de transporte verifica texto vacío, recepción y acuse; las de Windows verifican cálculo/preflight con MT5 simulado. El despliegue debe comprobar además lectura MT5 real y análisis a través de SSH sin enviar órdenes.

## Licencia y alcance

Este componente de integración se distribuye bajo **AGPL-3.0-only**, con [LICENSE](LICENSE), para mantener explícita su relación con forex-gold. El resto del fork conserva sus avisos/licencias originales. No se copió ni relicenció el motor upstream dentro del paquete MIT raíz; se importa el checkout separado, disponible en [MooreSi/forex-gold](https://github.com/MooreSi/forex-gold). Conservar fuentes y avisos al distribuir modificaciones del componente.

El servicio de investigación no contiene herramientas de envío al bróker. Poder analizar una señal no autoriza operar. Las aperturas automáticas requieren una política del propietario, con importes de riesgo, pérdida diaria y alcance definidos; indicar únicamente la moneda USD no define esos límites.
