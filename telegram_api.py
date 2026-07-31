"""Adapter del canal Telegram: valida el webhook, normaliza updates y envía respuestas.

Aísla la Bot API del resto del bot. app.py trabaja con (chat_id, texto, adjuntos)
y no sabe qué canal hay detrás; para cambiar de canal se reemplaza este módulo.

Reemplazó a Twilio WhatsApp porque el sandbox desconecta al participante cada 72hs
y obliga a re-enviar "join <code>".
"""

import hmac
import logging
import os

import httpx

log = logging.getLogger("bot.telegram")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# Telegram rechaza mensajes de más de 4096 caracteres: se parten antes de enviar.
MAX_MESSAGE_LEN = 4096
# getFile no sirve archivos de más de 20 MB.
MAX_FILE_BYTES = 20 * 1024 * 1024

TIMEOUT = 30

# Los updates traen la foto en varias resoluciones y sin mime; siempre son JPEG.
PHOTO_MIME = "image/jpeg"
# Las notas de voz vienen sin mime_type en algunos clientes.
VOICE_FALLBACK_MIME = "audio/ogg"
DOCUMENT_FALLBACK_MIME = "application/octet-stream"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Cualquiera que conozca la URL pública podría inyectar movimientos si no se
# valida el secret. Sólo se apaga para tests o desarrollo local.
VERIFY_WEBHOOK_SECRET = _env_flag("VERIFY_WEBHOOK_SECRET", True)


def valid_secret(received: str) -> bool:
    """Compara el header X-Telegram-Bot-Api-Secret-Token con el secret configurado.

    Telegram lo manda en cada request si se pasó `secret_token` en setWebhook.
    Reemplaza a la firma HMAC de Twilio: mismo objetivo, un décimo del código.
    """
    if not VERIFY_WEBHOOK_SECRET:
        return True
    if not WEBHOOK_SECRET:
        log.error("TELEGRAM_WEBHOOK_SECRET ausente: no se puede validar el webhook")
        return False
    # compare_digest y no ==: evita filtrar el secret por tiempo de comparación.
    return hmac.compare_digest(received or "", WEBHOOK_SECRET)


def extract_message(update: dict) -> dict | None:
    """Devuelve el message del update, o None si es un tipo que no procesamos."""
    if not isinstance(update, dict):
        return None
    message = update.get("message") or update.get("edited_message")
    return message if isinstance(message, dict) else None


def chat_id_of(message: dict) -> str:
    return str((message.get("chat") or {}).get("id") or "")


def message_ref(message: dict) -> str:
    """Identificador estable del mensaje, equivalente al MessageSid de Twilio.

    El backend lo usa como originRef para deduplicar, así que tiene que ser
    único por mensaje y repetible si Telegram reintenta el mismo update.
    """
    return f"tg-{chat_id_of(message)}-{message.get('message_id', '')}"


def extract_text(message: dict) -> str:
    """Texto del mensaje: `text` si es texto suelto, `caption` si acompaña un adjunto.

    Los comandos en grupos llegan como `/resumen@FinanzasBialyBot`; se normaliza el
    sufijo para que el ruteo de comandos no tenga que conocer el nombre del bot.
    """
    raw = (message.get("text") or message.get("caption") or "").strip()
    if raw.startswith("/"):
        head, _, rest = raw.partition(" ")
        head = head.split("@", 1)[0]
        raw = f"{head} {rest}".strip() if rest else head
    return raw


def extract_media_refs(message: dict) -> list[dict]:
    """Adjuntos del update, como [{"file_id", "mime"}].

    A diferencia de Twilio (que mandaba N adjuntos en un request), Telegram manda
    un update por archivo: un álbum llega como varios updates con el mismo
    media_group_id. Por eso la lista tiene 0 o 1 elemento y no hace falta truncar.
    """
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        # El array viene ordenado de menor a mayor resolución.
        return [{"file_id": photos[-1].get("file_id"), "mime": PHOTO_MIME}]

    document = message.get("document")
    if isinstance(document, dict):
        return [{
            "file_id": document.get("file_id"),
            "mime": document.get("mime_type") or DOCUMENT_FALLBACK_MIME,
        }]

    for key, fallback in (("voice", VOICE_FALLBACK_MIME), ("audio", VOICE_FALLBACK_MIME)):
        item = message.get(key)
        if isinstance(item, dict):
            return [{"file_id": item.get("file_id"), "mime": item.get("mime_type") or fallback}]

    return []


def _file_path(file_id: str) -> str:
    r = httpx.get(f"{API_BASE}/getFile", params={"file_id": file_id}, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise httpx.HTTPError(f"getFile falló: {payload.get('description')}")
    result = payload.get("result") or {}
    size = result.get("file_size") or 0
    if size > MAX_FILE_BYTES:
        raise httpx.HTTPError(f"archivo de {size} bytes, el máximo de getFile es {MAX_FILE_BYTES}")
    return result["file_path"]


def download_media(refs: list[dict]) -> list[dict]:
    """Descarga los adjuntos y los devuelve como [{"mime", "bytes"}].

    Dos saltos, a diferencia de Twilio que daba la URL directa: getFile resuelve el
    file_id a un path temporal y recién ahí se descarga. El path caduca en una hora.
    """
    items = []
    for ref in refs:
        path = _file_path(ref["file_id"])
        log.info("downloading media %s", path)
        r = httpx.get(f"{FILE_BASE}/{path}", timeout=TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        log.info("media downloaded: %d bytes", len(r.content))
        items.append({"mime": ref["mime"], "bytes": r.content})
    return items


def split_message(text: str) -> list[str]:
    """Parte un texto largo en chunks que Telegram acepte, cortando por línea."""
    if len(text) <= MAX_MESSAGE_LEN:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        # Una línea sola más larga que el límite se parte a lo bruto.
        while len(line) > MAX_MESSAGE_LEN:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_MESSAGE_LEN])
            line = line[MAX_MESSAGE_LEN:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def set_my_commands(commands: list[tuple[str, str]]) -> bool:
    """Publica el menú de comandos que Telegram autocompleta al escribir "/".

    Idempotente: se puede llamar en cada arranque. No lanza — que falle el menú
    no debe impedir que el bot levante.
    """
    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN ausente: no se publica el menú de comandos")
        return False
    payload = {"commands": [{"command": name.lstrip("/"), "description": desc}
                            for name, desc in commands]}
    try:
        r = httpx.post(f"{API_BASE}/setMyCommands", json=payload, timeout=TIMEOUT)
    except httpx.HTTPError:
        log.exception("no se pudo publicar el menú de comandos")
        return False
    if r.status_code != 200:
        log.error("setMyCommands %s: %s", r.status_code, r.text[:300])
        return False
    log.info("menú de comandos publicado (%d comandos)", len(commands))
    return True


def send_message(chat_id: str, text: str) -> None:
    """Envía una respuesta. No lanza: un fallo no debe cortar el resto de las respuestas."""
    for chunk in split_message(text):
        try:
            r = httpx.post(
                f"{API_BASE}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                log.error("sendMessage %s: %s", r.status_code, r.text[:300])
        except httpx.HTTPError:
            log.exception("no se pudo enviar la respuesta a %s", chat_id)
