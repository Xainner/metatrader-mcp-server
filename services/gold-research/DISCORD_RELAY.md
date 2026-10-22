# Reenvío de señales a Discord

Servicio independiente de la ejecución MT5 y de los acuses de Rinari. Lee evaluaciones nuevas de gold-research y reenvía únicamente las fuentes `telegram-fxtradingvision` y `whatsapp-main`. Filtra `noise`; incluye señales completas, incompletas/en revisión, mensajes de gestión y revisiones con su estado explícito. No envía cada publicación de los canales ni convierte una señal en autorización de trading.

Runtime privado `~/trading/discord-relay/`: `.env` (600) con `DISCORD_WEBHOOK_URL`, `config.json` con `research_database`, `data/deliveries.sqlite3` y `data/status.json`. Nunca subir el webhook, respuestas privadas o mensajes a Git. Servicio de usuario `trading-discord-relay.service` con el Python de gold-research. No abre puertos.

Inicializar con `python -m gold_research.discord_forwarder init` antes de iniciar el servicio. Marca el inicio con secuencia y fecha de recepción: excluye historial existente y eventos recibidos antes de habilitar el relay aunque sean procesados después. No reiniciar cursores ni borrar la cola para reparar fallos. Respaldar SQLite de forma consistente antes de cambios de esquema.

Cada embed incluye fuente, texto original (hasta 3500 caracteres), estado, revisión, tipo de evento, fecha original e identidad. Telegram añade el enlace al mensaje; no se descargan ni suben archivos. Las menciones están deshabilitadas y el texto va en un bloque literal. Ediciones/revocaciones detectadas generan un mensaje nuevo identificado, sin borrar los anteriores. Solo se reenvían las revisiones que los receptores consiguen capturar; las limitaciones de sus ventanas de sondeo permanecen.

El relay consulta cada segundo; los receptores consultan cada 5s y el procesador cada 1s. La latencia normal será de varios segundos, no instantánea ni garantizada. HTTP usa timeout de 10s y `wait=true`. Confirma envío únicamente con ID de mensaje recibido de Discord. Respeta 429/retry_after y headers de rate limit, persistiendo la espera y la cola tras reinicios. Un 401/403/404 exige revisar el webhook; se registra el fallo y se frena el destino.

Una desconexión después de enviar puede ocultar una entrega aceptada por Discord. Los timeouts, 5xx y envíos interrumpidos quedan `uncertain` y NO se reintentan automáticamente para evitar duplicados a ciegas. No se promete entrega exactamente una vez; revisar Discord/cola antes de una recuperación manual. Los fallos definitivos quedan `failed`; nuevos eventos y acuses de agentes no se pierden por ello.

`python -m gold_research.discord_forwarder test` envía una única prueba explícita, diferenciada de señales y operaciones, antes de iniciar el servicio. Su fila independiente impide repetirla al ejecutar el comando de nuevo. `... status` muestra heartbeat y recuentos sent/pending/failed/uncertain, sin exponer el secreto.

Referencias: [ejecución de webhooks](https://docs.discord.com/developers/resources/webhook#execute-webhook), [rate limits](https://docs.discord.com/developers/topics/rate-limits). Pruebas con transporte simulado; nunca usan un webhook real.
