# Integración WhatsApp Bot — Cambios en repo `Bot-finanzas`

Este documento describe los cambios a realizar **en el repositorio externo del bot** (`boris-riback/Bot-finanzas`) para que deje de usar Google Drive y pase a insertar los movimientos directamente en Supabase vía una Edge Function del ERP Bialystok.

Documento complementario: [whatsapp-bot-integration-erp.md](whatsapp-bot-integration-erp.md) — describe los cambios en el ERP (migrations, Edge Function, bucket de Storage, UI).

---

## 1. Contexto

El bot actual corre en Render y expone un webhook que Twilio llama cada vez que llega un mensaje de WhatsApp. El flujo actual es:

1. Twilio POSTea el mensaje al webhook del bot.
2. El bot chequea si el número está autorizado (lista en `NUMEROS_AUTORIZADOS` como env).
3. Envía el texto del mensaje a Claude API con un prompt que lo clasifica como egreso, ingreso o transferencia interna.
4. Genera un archivo `.txt` con los campos del movimiento y lo sube a una carpeta de Google Drive mediante una service account.
5. Si el mensaje traía un PDF adjunto, lo baja de Twilio y lo sube también a Drive.
6. Responde por WhatsApp con un resumen.

El nuevo flujo reemplaza los pasos 4 y 5 por llamadas HTTP a una Edge Function de Supabase, y mueve el control de autorización del bot a una tabla en la base (`finanzas.bot_phone_map`). El prompt de Claude cambia para que devuelva IDs exactos de catálogos en lugar de texto libre.

Además, cuando el mensaje de WhatsApp llegue con un **PDF o una imagen adjunta** (foto de ticket, factura, comprobante de transferencia, etc.), el bot va a **pasar ese archivo a Claude para que parsee los datos del movimiento** (monto, fecha, razón social, CUIT, número de comprobante, método de pago inferido) y los combine con el texto libre del mensaje. El adjunto también se sube a Supabase Storage y queda linkeado al movimiento.

---

## 2. Código a eliminar

- Toda la inicialización del cliente de Google Drive (lectura de credenciales de service account, construcción del cliente vía `googleapiclient`).
- Funciones `guardar_en_drive()`, `subir_pdf_a_drive()` o equivalentes.
- Lectura y parseo de las variables de entorno `GOOGLE_DRIVE_FOLDER_ID` y `GOOGLE_CREDENTIALS_JSON`.
- Archivos de credenciales `.json` dentro de `bot-archivos/` (y cualquier otro que contenga secretos de Google).
- La constante o variable `NUMEROS_AUTORIZADOS` y la función de chequeo asociada. El control de autorización ahora vive en la Edge Function: si un número no está en `bot_phone_map`, la función devuelve `403`.

---

## 3. Dependencias

Editar `requirements.txt`:

**Quitar:**
- `google-api-python-client`
- `google-auth`
- `google-auth-httplib2`
- `google-auth-oauthlib`

**Agregar:**
- `httpx>=0.27`
- `anthropic>=0.40` — SDK oficial, soporta `document` e `image` content blocks para parseo de PDFs e imágenes.

---

## 4. Módulo nuevo: `bialystok_client.py`

Cliente HTTP para la Edge Function. Encapsula las tres acciones que expone (`catalog`, `upload_attachment`, `ingest`).

```python
import httpx
import os
import base64
import time

BASE_URL = os.environ["BIALYSTOK_INGEST_URL"]
TOKEN = os.environ["BOT_INGEST_TOKEN"]

_HEADERS = {
    "x-bot-token": TOKEN,
    "Content-Type": "application/json",
}

_catalog_cache = {"data": None, "expires": 0}

def _post(payload: dict) -> dict:
    r = httpx.post(BASE_URL, json=payload, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_catalog(phone: str) -> dict:
    """Devuelve el catálogo de la organización asociada al número.
    Cachea por 1 hora en memoria del proceso."""
    now = time.time()
    if _catalog_cache["data"] and _catalog_cache["expires"] > now:
        return _catalog_cache["data"]
    data = _post({"action": "catalog", "phone": phone})
    _catalog_cache.update(data=data, expires=now + 3600)
    return data

def upload_attachment(phone: str, origin_ref: str, mime: str, content: bytes) -> str:
    """Sube un PDF/imagen al bucket de comprobantes y devuelve el path guardado."""
    b64 = base64.b64encode(content).decode()
    resp = _post({
        "action": "upload_attachment",
        "phone": phone,
        "originRef": origin_ref,
        "mime": mime,
        "base64": b64,
    })
    return resp["path"]

def ingest(payload: dict) -> dict:
    """Inserta un movimiento en finanzas.movements.
    Devuelve el registro creado o el existente (si hubo dedup)."""
    return _post({"action": "ingest", **payload})
```

---

## 5. Cambios en `app.py`

### 5.1 Flujo del webhook

El webhook incorpora un paso nuevo: cuando el mensaje trae un adjunto, el PDF o la imagen se incluyen en la llamada a Claude como `content block` de tipo `document` (para PDFs) o `image` (para PNG/JPG/WEBP/GIF). Claude entonces parsea los datos visibles del comprobante y los combina con el texto libre del usuario para armar el JSON final.

Pseudo-código del handler principal:

```python
import base64
from bialystok_client import fetch_catalog, upload_attachment, ingest

IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MIMES = {"application/pdf"}

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    phone = request.form["From"].replace("whatsapp:", "")
    body = request.form.get("Body", "")
    sid = request.form["MessageSid"]
    media_url = request.form.get("MediaUrl0")
    media_mime = request.form.get("MediaContentType0")

    # 1. Bajar adjunto (si vino) antes de llamar a Claude
    media_bytes = None
    if media_url:
        media_bytes = httpx.get(
            media_url,
            auth=(TWILIO_SID, TWILIO_TOKEN),
        ).content

    # 2. Catálogo vigente
    catalog = fetch_catalog(phone)

    # 3. Parseo con Claude — combina texto + adjunto (ver 5.2)
    parsed = claude_parse(body, catalog, media_bytes, media_mime)

    # 4. Subir adjunto a Supabase Storage (solo después de parseo OK)
    attachment_path = None
    if media_bytes:
        attachment_path = upload_attachment(phone, sid, media_mime, media_bytes)

    # 5. Ingest
    result = ingest({
        "phone": phone,
        "originRef": sid,
        "attachmentPath": attachment_path,
        **parsed,
    })

    # 6. Respuesta por WhatsApp
    if result["duplicated"]:
        return twilio_reply("Movimiento ya registrado previamente.")
    return twilio_reply(
        f"OK: {parsed['kind']} ${result['amount']:,.0f} "
        f"({result['status']})"
    )
```

### 5.2 Función `claude_parse` con soporte de adjuntos

Claude acepta PDFs e imágenes como parte del mismo mensaje. Hay que construir el array `content` dinámicamente:

```python
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def claude_parse(body: str, catalog: dict, media_bytes: bytes | None, media_mime: str | None) -> dict:
    content_blocks = []

    # Adjunto primero (si existe)
    if media_bytes and media_mime:
        b64 = base64.b64encode(media_bytes).decode()
        if media_mime in PDF_MIMES:
            content_blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            })
        elif media_mime in IMAGE_MIMES:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_mime,
                    "data": b64,
                },
            })
        # otros mimes: ignorar (se sube igual al Storage, pero Claude no lo mira)

    # Texto del prompt + catálogo + mensaje del usuario
    content_blocks.append({
        "type": "text",
        "text": build_prompt_text(catalog, body, has_attachment=bool(media_bytes)),
    })

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": content_blocks}],
    )

    return json.loads(resp.content[0].text)
```

### 5.3 Prompt de Claude

El prompt ahora distingue dos fuentes de información: el texto libre del usuario y el adjunto (ticket, factura, captura de transferencia). Las reglas definen cuál prevalece ante conflicto.

Template sugerido (`build_prompt_text`):

```
Sos un parser de movimientos financieros para un ERP.

Catálogo disponible para esta organización:
- businessUnits: {{businessUnits}}
- classifications: {{classifications}}
- concepts: {{concepts}}
- movementTypes: {{movementTypes}}
- paymentMethods: {{paymentMethods}}
- counterparties: {{counterparties}}

{{#if has_attachment}}
Se adjunta un comprobante (PDF o imagen). Mirá el documento y extraé:
- monto total (amount)
- fecha del comprobante (movementDate)
- razón social / proveedor / contraparte
- CUIT si está visible
- tipo y número de comprobante (Factura A/B/C, Recibo, Ticket, Transferencia, etc.)
- método de pago si está indicado
{{/if}}

Mensaje del usuario: "{{body}}"

Devolvé JSON exacto con estos campos:
{
  "kind": "egreso" | "ingreso",
  "classificationId": "<uuid del catálogo>",
  "conceptId": "<uuid del catálogo>",
  "movementTypeId": "<uuid>",
  "paymentMethodId": "<uuid>",
  "counterpartyId": "<uuid o null>",
  "counterpartyName": "<string si la contraparte no está en el catálogo, null si ya viene counterpartyId>",
  "businessUnitId": "<uuid>",
  "amount": <number>,
  "movementDate": "YYYY-MM-DD (hoy si no se menciona ni en texto ni en el adjunto)",
  "status": "pendiente" | "pagado",
  "receiptTypeId": "<uuid o null>",
  "receiptNumber": "<string o null>",
  "notes": "<string o null — incluí CUIT acá si lo viste>"
}

Reglas generales:
- Si el mensaje menciona "pagado", "cobrado", "ya pagué", "pagué" → status = "pagado".
- Si menciona "pendiente", "por pagar", "a pagar", o no especifica → status = "pendiente".
- Si la contraparte no existe en counterparties, devolvé counterpartyId: null y counterpartyName con el texto.
- Si no se menciona método de pago, usá "Efectivo".
- NUNCA inventes UUIDs que no estén en el catálogo.
- Si algún campo obligatorio no puede inferirse, respondé con un objeto {"error": "motivo"} en lugar del JSON de movimiento.

Reglas de resolución texto vs adjunto (cuando hay adjunto):
- Monto (amount): gana el adjunto salvo que el texto diga explícitamente otro número.
- Fecha: gana el adjunto salvo que el texto especifique otra.
- Contraparte / razón social: gana el adjunto.
- Método de pago y status: gana el texto del usuario (el adjunto puede decir "pagado" pero el usuario lo contradice).
- Número de comprobante: del adjunto.
- Si el adjunto es ilegible o no parece un comprobante, ignoralo y parseá solo el texto.
```

---

## 6. Variables de entorno

Editar la configuración del servicio en Render.

**Quitar:**
- `GOOGLE_DRIVE_FOLDER_ID`
- `GOOGLE_CREDENTIALS_JSON`
- `NUMEROS_AUTORIZADOS`

**Agregar:**
- `BIALYSTOK_INGEST_URL` — URL completa del endpoint, por ejemplo `https://<project>.supabase.co/functions/v1/whatsapp-bot-ingest`.
- `BOT_INGEST_TOKEN` — exactamente el mismo valor que el secret configurado en Supabase.

**Mantener:**
- `ANTHROPIC_API_KEY`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_NUMBER`

---

## 7. Archivos afectados

**Nuevos:**
- `bialystok_client.py`

**Modificados:**
- `app.py` (reemplazo del flujo completo, descripto arriba)
- `requirements.txt` (ajuste de dependencias)
- Configuración de env en Render

**Eliminar del repo:**
- Archivos de credenciales JSON de la service account de Google en `bot-archivos/`
- Cualquier import, función o constante relacionada con Google Drive

---

## 8. Orden de implementación sugerido

1. Confirmar que la Edge Function del ERP ya está desplegada en producción y que se puede llamar con `curl` desde afuera (ver verificación en el documento del ERP).
2. Obtener el `BOT_INGEST_TOKEN` generado para Supabase y cargarlo en las env de Render.
3. Crear `bialystok_client.py` y probarlo en local con un script:
   ```bash
   python -c "from bialystok_client import fetch_catalog; print(fetch_catalog('+549...'))"
   ```
4. Reescribir el prompt de Claude y testearlo con 10 mensajes de ejemplo (egreso con proveedor, ingreso, pagado vs pendiente, con/sin monto explícito, con/sin método de pago).
5. Reemplazar el webhook en `app.py`, conservando los comandos `/saldo`, `/reporte`, `/pendientes`, `/ayuda` como están por ahora (mientras no consumen Drive).
6. Actualizar `requirements.txt`, borrar imports y archivos de Drive.
7. Deploy en Render.
8. Test end-to-end enviando un mensaje real por WhatsApp y verificando que aparece el movimiento en el ERP.

---

## 9. Verificación

### 9.1 Cliente aislado

```python
from bialystok_client import fetch_catalog, ingest

# Catalog — debe devolver listas no vacías
catalog = fetch_catalog("+5491112345678")
assert catalog["classifications"], "catálogo vacío"

# Ingest de prueba usando los primeros IDs del catálogo
result = ingest({
    "phone": "+5491112345678",
    "originRef": "MANUAL_TEST_001",
    "kind": "egreso",
    "classificationId": catalog["classifications"][0]["id"],
    "conceptId": catalog["concepts"][0]["id"],
    "movementTypeId": catalog["movementTypes"][0]["id"],
    "paymentMethodId": catalog["paymentMethods"][0]["id"],
    "businessUnitId": catalog["businessUnits"][0]["id"],
    "amount": 1,
    "movementDate": "2026-04-15",
    "status": "pendiente",
    "counterpartyName": "Test manual",
})
print(result)
```

### 9.2 Parser Claude — texto solo

Preparar una batería de 10 mensajes de texto sin adjunto y correr `claude_parse` sin hacer ingest, solo para validar que devuelve JSON válido con IDs del catálogo en todos los casos.

Incluir variantes: egreso con proveedor conocido, egreso con proveedor nuevo, ingreso, pagado vs pendiente, con/sin monto explícito, con/sin método de pago.

### 9.3 Parser Claude — con adjuntos

Probar con cada tipo de adjunto que el bot va a recibir en la práctica:

- **Foto de ticket fiscal** (JPG, foto sacada con celular): Claude debe extraer monto, fecha, razón social/CUIT, número de ticket.
- **PDF de factura electrónica**: Claude debe extraer los mismos datos más el tipo de comprobante (A/B/C).
- **Captura de pantalla de transferencia** (PNG de app bancaria): Claude debe extraer monto, fecha, contraparte destino, detectar que el método de pago es "Transferencia" y status "pagado".
- **Imagen borrosa o no relacionada**: Claude debe ignorar el adjunto y parsear solo el texto; si el texto tampoco alcanza, devolver `{"error": "..."}`.

Para cada caso, comparar los campos extraídos contra los reales y ajustar el prompt si Claude falla consistentemente en alguno.

### 9.4 End-to-end

- Enviar por WhatsApp: `egreso 5000 a La Esquina efectivo pagado` (solo texto). Verificar que aparece el movimiento.
- Enviar una foto de ticket con el texto `"pagué este"`. Verificar que los datos vienen del ticket (monto, fecha, razón social) y `status="pagado"` del texto.
- Enviar un PDF de factura con el texto `"pendiente"`. Verificar que `status="pendiente"` aunque la factura indique otra cosa.
- En los tres casos, abrir `MovimientosPage` en el ERP, filtrar por origen "Bot WhatsApp" y verificar:
  - La fila aparece con los campos correctos.
  - El badge de origen bot es visible.
  - El link "Ver comprobante" abre el PDF/imagen.
- Reenviar exactamente el mismo mensaje (Twilio asigna un `MessageSid` nuevo, por lo que no hay dedup por origen; la dedup solo protege contra reintentos del propio Twilio con el mismo sid).

---

## 10. Consideraciones de costo y límites

- **Costo por token (Claude Sonnet 4.6):** imágenes y PDFs consumen más tokens que texto plano. Un ticket fiscal fotografiado ronda los 1.500–3.000 tokens de input; un PDF de una página, similar. A volúmenes bajos (decenas de mensajes por día) el impacto es marginal; monitorear con `usage` en las respuestas de la API si crece.
- **Límites de Claude:**
  - PDFs: máximo 32 MB, 100 páginas por documento.
  - Imágenes: recomendado <5 MB, resolución suficiente pero no excesiva (Claude reescala).
- **Límites de Twilio:** WhatsApp Business acepta archivos de hasta 16 MB. Si el cliente intenta mandar más, Twilio lo rechaza en origen.
- **Timeouts:** la llamada a Claude con adjunto puede tardar 10–20 segundos. El webhook de Twilio permite hasta 15 s antes de marcar error; si se acerca al límite, considerar responder de inmediato con "Procesando..." y procesar en background (Celery/RQ) con respuesta posterior por API de Twilio.

---

## 11. Fuera de alcance

- Migrar los `.txt` históricos de Google Drive a `movements`. Si se quiere, se puede hacer en un script one-shot aparte.
- Reescribir los comandos `/saldo`, `/reporte`, `/pendientes` para que lean de Supabase. Queda como iteración posterior.
- Edición o anulación de movimientos desde WhatsApp.
- Soporte para múltiples adjuntos en un mismo mensaje (`MediaUrl1`, `MediaUrl2`, etc.). El bot por ahora solo mira `MediaUrl0`.
