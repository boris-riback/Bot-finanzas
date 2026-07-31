# Bot Finanzas — Telegram + WhatsApp

Adapter que recibe mensajes, los interpreta con Claude y los manda a la Edge
Function `whatsapp-bot-ingest` del ERP Bialystok.

Corren **los dos canales a la vez**, cada uno en su ruta:

| Canal | Ruta | Estado |
|---|---|---|
| Telegram | `/webhook/telegram` | principal |
| WhatsApp (Twilio) | `/webhook` | secundario |

Telegram es el principal porque el sandbox de Twilio desconecta al participante
cada 72hs y obliga a re-enviar `join <code>`; salir de ahí exige verificación de
negocio en Meta. WhatsApp queda vivo para no perder la vía conocida.

**Apagar WhatsApp** es sacar las tres `TWILIO_*` de Render: el canal se desactiva
solo y `/webhook` empieza a devolver 503, sin tocar código.

## Variables de entorno (configurar en Render)

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token que da BotFather (`123456789:AAH...`) |
| `TELEGRAM_WEBHOOK_SECRET` | String random larga. Telegram la devuelve en cada request |
| `TWILIO_ACCOUNT_SID` | Account SID de Twilio. Sin esto el canal WhatsApp queda apagado |
| `TWILIO_AUTH_TOKEN` | Auth Token de Twilio (además valida la firma del webhook) |
| `TWILIO_WHATSAPP_NUMBER` | Número de Twilio (ej: `+14155238886`) |
| `ANTHROPIC_API_KEY` | Tu API Key de Anthropic |
| `OPENAI_API_KEY` | API Key de OpenAI (transcripción de audios con Whisper) |
| `BIALYSTOK_INGEST_URL` | URL de la Edge Function `whatsapp-bot-ingest` |
| `BOT_INGEST_TOKEN` | Token compartido con la Edge Function (header `x-bot-token`) |
| `VERIFY_WEBHOOK_SECRET` | `true` por defecto. Sólo poner `false` en desarrollo local |
| `VERIFY_TWILIO_SIGNATURE` | `true` por defecto. Sólo poner `false` en desarrollo local |
| `EXPOSE_ERROR_DETAIL` | `false` por defecto. `true` muestra el error crudo del backend en el chat |

Los remitentes autorizados se resuelven en la Edge Function contra
`finanzas.bot_phone_map`, no en el bot. Un `chat_id` que no esté mapeado recibe
403 — que cualquiera pueda escribirle al bot no significa que pueda cargar nada.

## Arquitectura

```
Telegram → /webhook/telegram ─┐
                              ├→ process_async(channel, …) → parser (Claude)
WhatsApp → /webhook ──────────┘                                    ↓
                                              bialystok_client → Edge Function
                                                                   ↓
                                                            finanzas.movements
```

- `telegram_api.py` / `twilio_api.py` — todo lo específico de cada canal: validan
  el request, normalizan el mensaje, descargan adjuntos, envían respuestas.
- `app.py` — ruteo y lógica de conversación. Trabaja con `(remitente, texto,
  adjuntos)` y no sabe por qué canal entró el mensaje.
- `bialystok_client.py` — cliente HTTP de la Edge Function. Agnóstico del canal.

Los dos adapters exponen la **misma interfaz** para lo que consume
`process_async`: `download_media(refs)` y `send_message(destino, texto)`. Agregar
un canal nuevo es escribir un módulo con esas dos funciones y una ruta.

El identificador del remitente (teléfono o `chat_id`) viaja en el campo `phone`
del payload: la Edge Function busca en `bot_phone_map` por `phone` **o** por
`telegram_chat_id` y siempre devuelve el `phone` canónico, que es la clave de
scoping de las tablas `bot_pending_*`.

**Consecuencia de tener dos canales**: los pendientes (selección de proveedor,
transferencias, liquidaciones) viven en el backend scopeados por el `phone`
canónico, así que **se comparten entre canales** — podés arrancar por WhatsApp y
confirmar por Telegram. El historial de conversación, en cambio, vive en memoria
del proceso indexado por el identificador crudo: **no se comparte**.

## Seguridad de los webhooks

| Ruta | Validación |
|---|---|
| `/webhook/telegram` | header `X-Telegram-Bot-Api-Secret-Token` vs `TELEGRAM_WEBHOOK_SECRET`, con `hmac.compare_digest` |
| `/webhook` | firma `X-Twilio-Signature` sobre el form del request |

Sin eso, cualquiera que conozca la URL pública puede inyectar movimientos en el ERP.

La URL que firma Twilio se reconstruye desde `X-Forwarded-Proto` /
`X-Forwarded-Host` porque Render termina TLS en su proxy: usar `request.url`
directo daría `http://` y toda firma válida sería rechazada.

El webhook de Telegram **siempre responde 200**, incluso ante error: un no-200
hace que Telegram reintente el update y se duplicaría el movimiento. Los errores
se le informan al usuario desde el thread de procesamiento.

## Formato de mensajes

### Registrar egreso
```
MGB 5000 efectivo
MGB - 5000 - efectivo
egreso - MGB - 5000 - transferencia - insumos
```

### Registrar ingreso
```
ingreso servicio del dia 50000 nave
ingreso - alquiler chopera - 110000 - mercadopago
```

### Comandos
```
/ayuda      menú de comandos
/resumen    pagos pendientes próximos 7 y 30 días
/adelanto <nombre> <monto> [nota]
/liquidar <nombre>
/cancelar
```

`/start` y `/help` son alias de `/ayuda`.

Un comando desconocido no se manda al parser: contesta con el índice de ayuda.
Así "/pagos" no termina interpretado como un movimiento.

### Ayuda

`/ayuda` muestra un índice corto; el detalle se pide por tema:

```
/ayuda cargar          registrar egresos e ingresos
/ayuda adjuntos        fotos, PDF y audios
/ayuda proveedores     menú cuando no reconoce la contraparte
/ayuda corregir        arreglar lo último cargado
/ayuda transferencias  mover plata entre cajas
/ayuda rrhh            adelantos y liquidaciones
/ayuda consultas       qué se le puede preguntar
/ayuda comandos        lista seca
```

Los temas aceptan sinónimos (`/ayuda sueldos` → `rrhh`, `/ayuda fotos` →
`adjuntos`). El texto vive en `HELP_INDEX` / `HELP_TOPICS` en `app.py`.

**Texto plano a propósito**: `send_message` no manda `parse_mode`, así que
cualquier `*negrita*` se vería con los asteriscos literales.

### Menú de comandos de Telegram

`BOT_COMMANDS` en `app.py` se publica con `setMyCommands` en cada arranque, en un
thread para no bloquear el boot. Agregar un comando ahí alcanza para que aparezca
en el autocompletado del chat al escribir `/`.

### Adjuntos

Mandá **foto, PDF o audio** con texto libre para registrar un movimiento.

Mandá los comprobantes como **Archivo**, no como Foto: Telegram comprime las
fotos y un ticket con letra chica se vuelve ilegible para el OCR. Como documento
va intacto y encima llega el `mime_type` real.

Telegram manda un update por archivo — un álbum llega como varios updates — así
que cada adjunto genera su propio movimiento.

## Deploy en Render

1. Push a GitHub
2. En Render: New Web Service → conectar repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
5. Agregar las variables de entorno
6. Deploy

**Free tier**: el servicio se duerme por inactividad y el primer mensaje tarda
30-50s en despertar. Con Twilio esto era fatal (timeout de 3s); con Telegram sólo
es molesto.

## Registrar los webhooks

### Telegram

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://tu-app.onrender.com/webhook/telegram","secret_token":"<SECRET>","allowed_updates":["message"]}'
```

Verificar:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

Con `last_error_message` vacío y `pending_update_count` en 0 está andando.

### Twilio

Twilio Console → Messaging → WhatsApp Sandbox → "When a message comes in":

```
https://tu-app.onrender.com/webhook
```

Se dejó en `/webhook` (y no en `/webhook/twilio`) justamente para no tener que
reconfigurar la consola.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```
