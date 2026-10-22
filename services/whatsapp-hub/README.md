# WhatsApp → señales de oro → Rinari

Receptor de solo lectura con `@wppconnect-team/wppconnect` **2.3.5**, Chromium y Node 24. La cuenta vinculada debe pertenecer al grupo de señales de la comunidad. No se crea un bot de WhatsApp Business ni se usa un token de Telegram: se vincula la sesión con QR.

## Componentes

- `src/server.mjs`: QR y estado por HTTP privado; escucha mensajes nuevos, ediciones y revocaciones. No ofrece funciones para enviar mensajes, unirse a grupos ni operar.
- `src/store.mjs`: SQLite/WAL con sincronización FULL. Filtra por grupo, autor opcional y fecha mínima **antes** de guardar. IDs originales, texto original, fechas y revisiones. Entregas repetidas consecutivas no duplican el evento.
- `config.example.json`: configuración inicial deshabilitada y sin grupos. `allowed_group_ids` requiere IDs exactos `…@g.us`; nunca nombres ambiguos. `allowed_author_ids: []` observa todos los autores del grupo autorizado, sin concederles autoridad para operar.
- `data/status.json`: heartbeat sin textos, números ni QR. `gold-research.get_research_status` lo muestra, incluyendo indicador de antigüedad.

La selección de grupo, sesión del navegador, tokens, mensajes y configuración real quedan fuera de Git en `~/trading/whatsapp-hub`, permisos 700/600. No copiar esta carpeta al repositorio.

## Instalación

Construir con `docker build -t trading/whatsapp-hub:0.1.0 services/whatsapp-hub`. La descarga de Chromium de Puppeteer se desactiva; se instala Chromium desde Debian. La imagen usa el usuario `node` (UID 1000); el directorio montado debe pertenecer a ese UID.

Publicar exclusivamente `127.0.0.1:18982:3000`, montar el runtime en `/data`, limitar CPU/memoria y usar reinicio supervisado. No montar Docker socket ni carpetas de otros proyectos y no asignar GPU. El navegador usa `--no-sandbox` dentro del contenedor sin privilegios, sin capacidades Linux y con `no-new-privileges`; no se considera una barrera contra todo contenido hostil.

Desde el ordenador:

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18982:127.0.0.1:18982 server2
```

Abrir `http://127.0.0.1:18982/`. En WhatsApp: **Dispositivos vinculados → Vincular dispositivo**, escanear el QR. El QR se mantiene solo en memoria, caduca en la interfaz y no se escribe en logs. Después de vincular, la página muestra solo nombres/IDs de grupos para que el propietario elija el de señales.

El operador edita `config.json` con el ID autorizado, `enabled: true` y `capture_after` igual al momento de habilitar. Se recarga por evento. Cambiar `source_id` requiere una instalación nueva: no reutilizar la misma base con otra identidad. Las modificaciones inválidas detienen el receptor para evitar una allowlist indeterminada.

## Integración existente

En la configuración privada de `gold-research`, conservar `raw_database` y `source_id` históricos y añadir:

```json
{
  "sources": [
    {"source_id": "telegram-main", "raw_database": "/home/USER/trading/signal-hub/data/signals.sqlite3"},
    {"source_id": "whatsapp-main", "raw_database": "/home/USER/trading/whatsapp-hub/data/signals.sqlite3", "status_file": "/home/USER/trading/whatsapp-hub/data/status.json"}
  ]
}
```

Antes de migrar: detener `gold-research.service` y respaldar SQLite usando su API de backup. `Inbox.initialize` añade la tabla `imports` y reconstruye identidades desde eventos existentes. Conserva IDs públicos y recibos; las nuevas secuencias del digest son globales, con cursor independiente para cada fuente. Reiniciar el procesador. Rinari conserva su conector `gold-research`; usar `peek_signal_digest`, `get_signal`, `search_gold_signals` y acuses explícitos.

## Español y límites

Ejemplo sintético soportado:

```text
COMPRA ORO XAUUSDm
Entrada: 2500,50 - 2502,00
SL: 2495,00
Objetivo 1: 2510,50
TP2: 2515,00
```

También VENTA, zona de entrada, stop loss y TP. No inventa entrada ni SL/TP; valida el lado de los niveles con forex-gold y conserva errores. Comas decimales con tres cifras o separadores mezclados quedan en revisión. Órdenes de gestión, revocaciones y mensajes sin correlación explícita no se ejecutan. Imágenes, audio y documentos quedan en cuarentena; no hay OCR ni transcripción. Un mensaje parseado nunca autoriza una operación.

WPPConnect usa WhatsApp Web y su disponibilidad depende de cambios de WhatsApp y de la sesión vinculada. No garantiza recuperación de mensajes perdidos durante desconexiones: no se recorre historial automáticamente ni se reactivan señales antiguas. El watchdog reinicia tras tres fallos de conexión a intervalos de 30 s; los periodos sin conexión deben revisarse. Grupos privados y chats no autorizados no se guardan, pero la sesión de WhatsApp Web vinculada tiene las capacidades de la cuenta.

`npm audit` del lock inicial registra 9 avisos altos transitivos por `extract-zip` y `basic-ftp` en la cadena de descarga/proxy de Puppeteer. No hay corrección directa compatible publicada para todos: no degradar WPPConnect con `audit fix --force`. La instalación usa Chromium del sistema, omite las descargas de Puppeteer y no configura proxy/PAC/FTP ni recibe archivos de instalación por la interfaz. Mantener la revisión de dependencias como pendiente conocido.

## Verificación

`npm test` usa mensajes ficticios y SQLite en memoria, sin WhatsApp. Las pruebas de `services/gold-research/tests/test_multisource_spanish.py` verifican español, niveles inválidos, colisiones entre fuentes y recibos conservados tras migración. Una prueba real requiere QR, grupo autorizado y observar un mensaje nuevo; no implica enviar mensajes desde el receptor ni colocar operaciones.

WPPConnect se distribuye bajo LGPL-3.0-or-later; este adaptador está bajo la licencia MIT del repositorio. El componente `gold-research` conserva su licencia AGPL-3.0-only independiente.
