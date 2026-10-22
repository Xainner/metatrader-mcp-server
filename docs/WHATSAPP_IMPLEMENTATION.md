# WhatsApp: implementación y validación

Se añadió `services/whatsapp-hub` al centro de señales. WPPConnect/Chromium se ejecutan en un contenedor limitado de CPU/memoria, con sesión persistente privada, bloqueo exclusivo del perfil y servicio de usuario systemd. La pantalla de vinculación se expone solo por loopback y túnel SSH; QR en memoria, tamaño nítido y margen blanco.

Hay selección explícita por ID para grupos/comunidades y para canales `@newsletter`. La selección no implica seguir canales ni unirse a grupos. El receptor no ofrece envío, edición de mensajes remotos, administración ni ejecución financiera. Los mensajes originales, revisiones, fechas y revocaciones se conservan en SQLite; se rechazan fuentes no autorizadas y mensajes propios antes de persistir.

Canales: eventos más sondeo de hasta 50 publicaciones cada 5 s. Filtra las anteriores a la fecha de habilitación. Reporta autenticación, preparación, resultado/fecha del sondeo y heartbeat. El estado de socket conectado no sustituye la comprobación de autenticación. Reiniciar el contenedor conserva la sesión y elimina solo enlaces efímeros de bloqueo de Chromium, bajo exclusión del runtime.

`gold-research` mantiene cursores por fuente y secuencias globales en el digest. La migración reconstruye identidades sin renumerar recibos existentes. El parser español exige entrada/SL/TP explícitos y valida niveles con forex-gold; números ambiguos, gestión, campos faltantes y medios se revisan. Rinari continúa usando `gold-research` y `metatrader-demo`.

## Validación realizada

- Cuatro pruebas Node: filtros/autoría/fecha, deduplicación y revisiones A→B→A, persistencia/revocaciones, allowlist independiente de canales.
- Dieciséis pruebas Python: parser español/inglés, datos de mercado, colisiones entre fuentes, migración/recibos y transportes; Windows todas pasan, Linux tres pruebas del adaptador Windows omitidas.
- MCP real por SSH: siete herramientas, parsing español y estado de ambas fuentes.
- Instalación: autenticación y preparación verificadas; un único canal autorizado seleccionado por coincidencia exacta del nombre aportado por el propietario. Lectura de una publicación anterior a la habilitación verificada y filtrada. Sin mensajes enviados ni órdenes MT5.

## Pendientes y límites

- Observar una publicación nueva del canal para verificar la entrega real a SQLite → digest; la lectura de publicaciones antiguas y las pruebas sintéticas no prueban ese evento futuro.
- WhatsApp Web puede cambiar o desvincular la sesión. No hay garantía de recuperar huecos ni ráfagas de más de 50 publicaciones durante desconexiones; revisar antigüedad antes de decidir.
- Imágenes/audio/documentos quedan en revisión; sin OCR ni transcripción. Nuevos formatos de señales requieren ejemplos y pruebas; el parser no inventa campos.
- No se armaron reglas de Agency ni límites automáticos de ejecución. El ritmo de sondeo no implica decisiones u órdenes cada 5 s.
- El README del receptor documenta avisos transitivos de npm/Puppeteer y aislamiento del navegador. Mantener versiones revisadas y no usar degradaciones forzadas como arreglo.

Datos de sesión, IDs privados, configuración y mensajes reales se mantienen fuera de Git. Consultar el mapa privado de `~/trading/AGENTS.md` para operación de esta instalación.
