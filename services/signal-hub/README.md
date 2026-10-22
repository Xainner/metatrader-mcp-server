# Signal Hub 0.1 — Telegram en observación

Primera pieza ejecutable del [estudio de automatización](../../docs/AUTOMATION_IMPLEMENTATION_STUDY.md). Paquete independiente del MCP MT5: se instala en Linux sin `MetaTrader5`, no usa GPU y no envía mensajes de Telegram ni operaciones al bróker.

## Qué implementa

- Bot API con long polling: mensajes y ediciones de canales/grupos permitidos.
- SQLite/WAL: texto/caption, IDs de origen, respuestas, revisiones, hashes y timestamps UTC.
- Eventos y siguiente offset de Telegram confirmados en una misma transacción. Repetir `update_id` no duplica el evento; otra señal con el mismo texto sí se conserva.
- Lista explícita de chat IDs; los demás mensajes no se guardan. Bloqueo local de un segundo poller y rechazo de un webhook existente, sin borrarlo.
- Vinculación de base de datos a `source_id` y bot ID. No reutilizar un token de un bot que ya atiende otro servicio.
- Tres herramientas MCP **de lectura** por SSH/stdin: `get_signal_hub_status`, `list_signal_events`, `get_signal_event`.
- Sin puerto de red entrante. Las credenciales viajan únicamente a la API HTTPS de Telegram y permanecen fuera del repositorio.

No implementa todavía parser financiero, validación de señales, digest con acuse, TradingView, ejecución, supervisor de P&L o reglas de Agency. Todo evento queda en `state=observed`; no confundirlo con una orden válida ni con los contratos futuros `v1` del estudio. Captions se conservan como `media_or_service`; no se descarga ni interpreta la imagen/audio.

## Disposición recomendada en server2

```text
~/trading/
  AGENTS.md
  repos/metatrader-mcp-server/         # Checkout de código
  signal-hub/
    .venv/                            # Entorno independiente
    config.json                       # Configuración no secreta
    .env                              # Token privado; modo 600
    data/signals.sqlite3              # Eventos privados y offset
    data/poller.lock
    requirements.installed.txt         # Versiones del despliegue
  docs/                               # Mapa/operación local y estudios
  backups/                            # Copias del mapa antes de modificarlo
```

## Instalación manual

Ejecutar como propietario, sin instalar el proyecto MT5 raíz en Linux:

```sh
umask 077
mkdir -p ~/trading/signal-hub
python3 -m venv ~/trading/signal-hub/.venv
~/trading/signal-hub/.venv/bin/python -m pip install -e "$HOME/trading/repos/metatrader-mcp-server/services/signal-hub[mcp]"
cp ~/trading/repos/metatrader-mcp-server/services/signal-hub/config.example.json ~/trading/signal-hub/config.json
cp ~/trading/repos/metatrader-mcp-server/services/signal-hub/.env.example ~/trading/signal-hub/.env
chmod 600 ~/trading/signal-hub/config.json ~/trading/signal-hub/.env
~/trading/signal-hub/.venv/bin/python -m trading_signal_hub init
mkdir -p ~/.config/systemd/user
cp ~/trading/repos/metatrader-mcp-server/services/signal-hub/deploy/trading-signal-hub.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now trading-signal-hub
```

Copiar ejemplos solo en una instalación nueva; no sobrescribir configuración existente. El servicio se queda esperando si faltan token, allowlist o `enabled=true`. `loginctl enable-linger "$USER"` permite que el gestor de servicios del usuario siga activo sin sesión SSH y arranque tras reinicio; comprobar `loginctl show-user "$USER" -p Linger`. Si el sistema lo deniega, se necesita habilitar ese ajuste con permisos del host.

## Conectar tu bot

1. Crear un bot dedicado mediante [BotFather](https://t.me/BotFather). Editar `~/trading/signal-hub/.env` en el servidor y completar `TELEGRAM_BOT_TOKEN=...`, sin comillas. No pegar el token en chats, comandos que queden en historial o archivos versionados.
2. Añadirlo al canal/grupo. En grupos, configurar los permisos/privacy mode necesarios para que reciba señales normales; en canales, darle únicamente los permisos necesarios para incorporarlo y leer posts. [FAQ oficial de recepción](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).
3. Con `enabled=false`, detener el receptor y hacer que llegue un mensaje al canal. Obtener IDs sin mostrar su contenido:

```sh
systemctl --user stop trading-signal-hub
~/trading/signal-hub/.venv/bin/python -m trading_signal_hub discover-chats
```

La exploración es una acción local del operador: muestra IDs/tipos/títulos, no mensajes, y no avanza el offset. Puede no ver chats sin updates pendientes. No borra webhooks ni toma control de un bot usado por otro sistema.

4. Añadir los IDs numéricos correctos a `allowed_chat_ids` en `config.json` y cambiar `enabled` a `true`. Reiniciar el servicio. El polling normal conserva la configuración cargada hasta reiniciar; no asumir que editar el archivo detiene un poller ya activo.

```sh
systemctl --user restart trading-signal-hub
~/trading/signal-hub/.venv/bin/python -m trading_signal_hub status
journalctl --user -u trading-signal-hub -n 30 --no-pager
~/trading/signal-hub/.venv/bin/python -m trading_signal_hub events --limit 5
```

`configured=true` no significa conectado: comprobar `receiver_state`, `last_poll_at` y errores. El timeout de long polling es espera máxima cuando no hay novedades, no un intervalo fijo de 25 segundos por mensaje. El siguiente offset solo se envía después del commit durable. La Bot API no ofrece aquí un historial arbitrario ni una garantía general de eventos de borrado: no afirmar que se han capturado todos los mensajes anteriores. [Bot API: getUpdates](https://core.telegram.org/bots/api#getupdates).

## Comprobar acceso a nuevas fuentes

`python -m trading_signal_hub check-chats --chat-id=-1001234567890` comprueba identidad, tipo y membresía del propio bot mediante `getMe`, `getChat` y `getChatMember`. No consume `getUpdates`, no mueve el offset, no envía mensajes y no añade el bot a chats. Puede ejecutarse con el receptor activo. Una entrada en la lista permitida no prueba acceso.

Usar IDs exactos de Bot API. Los IDs de canales/supergrupos llevan signo negativo; no añadir un segundo prefijo `100` si ya existe. Si el propietario aporta un número positivo con apariencia de ID de canal, su conversión a negativo sigue pendiente de confirmación por Telegram; un HTTP 400 puede indicar ID incorrecto o falta de acceso, no identifica por sí solo cuál de ellos.

Para recibir publicaciones el bot debe tener acceso real al canal/grupo. En grupos, además necesita visibilidad de mensajes normales por permisos/privacidad; mensajes escritos por otros bots tienen limitaciones de recepción documentadas por Telegram. Si el propietario solo accede con su cuenta personal y no puede incorporar un bot, se requiere un receptor de sesión de usuario separado; este paquete no inicia ni suplanta esa sesión. [FAQ oficial](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

Los textos en inglés pasan al parser/validador y digest del paquete hermano `gold-research`. Que el idioma esté soportado no garantiza que cualquier formato tenga entrada, SL y TP completos; consultar estados y revisiones y mantener separado el mandato de ejecución.

## Rinari: conector separado de lectura

Fragmento MCP de ejemplo para un cliente en Windows con el alias SSH configurado. No crea reglas de Agency ni sustituye `metatrader-demo`:

```json
{
  "mcpServers": {
    "trading-signals": {
      "command": "ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "server2",
        "/home/xainner/trading/signal-hub/.venv/bin/python", "-m", "trading_signal_hub", "mcp"
      ]
    }
  }
}
```

El SSH autentica el acceso; no transmitir el bot token al cliente MCP. El servidor de lectura abre SQLite en modo `ro` y no tiene herramientas de escritura. Eso no restringe un acceso SSH general del usuario: los permisos de Agency y los del sistema deben configurarse por separado. El texto recibido sigue siendo contenido externo no confiable.

`list_signal_events` pagina sin consumir ni acusar: conservar `next_cursor` o consultar un rango explícito. No usar todavía una regla fija de Agency que llame indefinidamente con cursor 0: releería los mismos eventos. El digest/acuse durable del estudio es otra entrega pendiente.

## Pruebas y recuperación

```sh
cd ~/trading/repos/metatrader-mcp-server/services/signal-hub
. ~/trading/signal-hub/.venv/bin/activate
python -m unittest discover -s tests -v
```

Los tests usan eventos sintéticos, API simulada y sesiones MCP locales; no consultan Telegram ni operan MT5. En Linux incluyen exclusión de pollers. No insertar fixtures en la base operativa.

Para recuperar: detener el receptor, inspeccionar el estado sin publicar textos privados y reiniciar conservando base y offset. No borrar la base para resolver un error ni restaurar una copia anterior sin entender posibles relecturas. Para backup consistente usar la API `sqlite3.Connection.backup` o detener todos los escritores antes de copiar; no copiar únicamente el `.sqlite3` activo ignorando WAL. Preservar permisos 700/600 y definir retención antes de acumular canales reales. La primera versión no borra automáticamente historial.
