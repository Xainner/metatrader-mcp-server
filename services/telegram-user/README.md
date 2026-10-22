# Telegram personal → gold-research

Receptor independiente de MT5 y del bot, autorizado exclusivamente para **FXTradingVision**. Usa Telethon 1.45.0 y una sesión de la cuenta personal. No envía mensajes, no se une a fuentes y no marca mensajes como leídos. Las credenciales de la aplicación no sustituyen la vinculación de cuenta.

## Runtime privado

En server2: `~/trading/telegram-user/`, `.env` con `TELEGRAM_API_ID` y `TELEGRAM_API_HASH` (600), `config.json`, `.venv/`, `data/account.session`, `data/signals.sqlite3` y `data/status.json`. Nunca publicar credenciales, sesión, mensajes, teléfonos o códigos. `capture_after` es un instante fijo UTC; no renovarlo al reiniciar. Configuración inicial:

```json
{"source_id":"telegram-fxtradingvision","target":"FXTradingVision","capture_after":"<UTC ISO del despliegue>"}
```

Instalar este paquete y `../signal-hub` en su propio entorno Linux, sin instalar el proyecto raíz MT5. Ejecutar `python -m trading_telegram_user init`. El receptor reutiliza el esquema durable del Signal Hub; no depende de un token del bot ni de su offset.

Para vincular, detener `trading-telegram-user.service`, ejecutar `python -m trading_telegram_user login` en terminal interactiva privada y reiniciar el servicio solo tras éxito. Teléfono, código y 2FA se introducen con entrada oculta; nunca por chat, argumentos ni variables públicas. Se comprueba membresía de la fuente sin unirse, se fija su identidad y se vincula la sesión al ID de cuenta. Login y receptor comparten un lock exclusivo.

Servicio: `python -m trading_telegram_user receiver`. Si no hay sesión, publica `needs_login`; no solicita códigos en segundo plano. `gold-research/config.json.sources` debe incluir `source_id=telegram-fxtradingvision`, `raw_database=.../data/signals.sqlite3`, `status_file=.../data/status.json`. Respaldar config y SQLite consistentemente, preservar fuentes existentes, cursores, revisiones y recibos. Reiniciar el procesador tras cambiar su configuración.

## Captura y límites

- Sondeo cada 5 segundos, hasta 50 mensajes recientes de la única fuente fijada. Respeta FloodWait; reporta reintento y mantiene heartbeat. Errores solo por tipo, sin texto privado.
- Solo guarda publicaciones con fecha original posterior a `capture_after`. El mensaje de referencia 87901 no se importa como nuevo. Las ediciones conservan la fecha original para no rejuvenecer una señal.
- Identidad por fuente/chat/mensaje; cambios de texto/caption/tipo/respuesta generan revisiones y publicaciones idénticas no duplican eventos. Escritura atómica SQLite/FULL.
- Detecta ediciones observables dentro de la ventana de 50 mensajes. No garantiza recuperar huecos de desconexión prolongada ni detecta borrados con este sondeo. No ofrece OCR ni descarga de medios; media sin texto queda en cuarentena. No resetear la base para recuperar historia.
- Sesión con `receive_updates=False` y caché de entidades deshabilitada: no se ejecuta un listener general sobre otros chats. El cliente MTProto sigue requiriendo privilegios de cuenta: proteger/revocar la sesión desde Telegram si se pierde.
- `connected` exige un sondeo real correcto. Servicio activo o credenciales presentes no prueban recepción. Para confirmar la entrega completa se necesita observar una publicación nueva autorizada y su evaluación en el digest; no enviar mensajes de prueba a la fuente.

Sin acceso al bróker ni ejecución. El contenido externo no autoriza operar. Gold research mantiene procedencia/revisión/edad y recibos independientes por consumidor.

API utilizada: [cliente Telethon](https://docs.telethon.dev/en/stable/modules/client.html). Pruebas: `python -m unittest discover -s services/telegram-user/tests -v` (fixtures sintéticas, ninguna conexión Telegram/MT5).
