"""Adapter del canal Twilio WhatsApp: valida la firma, normaliza el form y envía respuestas.

Misma interfaz que `telegram_api` para lo que consume `process_async`:
`download_media(refs)` y `send_message(destino, texto)`. Eso permite que la lógica
de conversación no sepa por qué canal entró el mensaje.

El sandbox de Twilio desconecta al participante cada 72hs y obliga a re-enviar
"join <code>". Por eso el canal principal es Telegram; este queda como secundario.
Sin las TWILIO_* configuradas el canal se apaga solo (ver `enabled()`).
"""

import logging
import os

import httpx
from flask import request

log = logging.getLogger("bot.twilio")

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")

# Twilio manda N adjuntos en un mismo request (a diferencia de Telegram, que manda
# un update por archivo), así que acá sí hace falta un tope.
MAX_MEDIA_PER_MESSAGE = 10
TIMEOUT = 30


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Cualquiera que conozca la URL pública podría inyectar movimientos si no se
# valida la firma. Sólo se apaga para tests o desarrollo local.
VERIFY_TWILIO_SIGNATURE = _env_flag("VERIFY_TWILIO_SIGNATURE", True)

_client = None
_validator = None

# Import perezoso y tolerante: si el paquete no está o faltan credenciales, el
# canal queda apagado en vez de romper el boot del bot entero.
if ACCOUNT_SID and AUTH_TOKEN:
    try:
        from twilio.request_validator import RequestValidator
        from twilio.rest import Client as TwilioClient

        _client = TwilioClient(ACCOUNT_SID, AUTH_TOKEN)
        _validator = RequestValidator(AUTH_TOKEN)
    except Exception:
        log.exception("no se pudo inicializar Twilio: el canal WhatsApp queda apagado")


def enabled() -> bool:
    """True si el canal está configurado y puede recibir y responder mensajes."""
    return bool(_client and _validator and WHATSAPP_NUMBER)


def public_request_url() -> str:
    """URL que Twilio firmó.

    Render (y cualquier proxy que termine TLS) deja llegar request.url como
    http://, mientras que Twilio firmó la https:// configurada en la consola.
    Reconstruirla desde los headers X-Forwarded-* es lo que evita que toda
    firma válida sea rechazada.
    """
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
    url = f"{proto}://{host}{request.path}"
    if request.query_string:
        url = f"{url}?{request.query_string.decode()}"
    return url


def valid_signature() -> bool:
    """Valida el header X-Twilio-Signature contra el form del request."""
    if not VERIFY_TWILIO_SIGNATURE:
        return True
    if not _validator:
        log.error("TWILIO_AUTH_TOKEN ausente: no se puede validar la firma")
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False
    return _validator.validate(public_request_url(), request.form.to_dict(), signature)


def sender_of() -> str:
    """Teléfono del remitente, sin el prefijo `whatsapp:`.

    Es el identificador que viaja en el campo `phone` del payload al backend, y
    coincide con la clave canónica de `finanzas.bot_phone_map`.
    """
    return request.values.get("From", "").replace("whatsapp:", "")


def extract_text() -> str:
    return (request.values.get("Body") or "").strip()


def message_ref() -> str:
    """MessageSid: el backend lo usa como originRef para deduplicar."""
    return request.values.get("MessageSid", "")


def num_media() -> int:
    try:
        return int(request.values.get("NumMedia") or 0)
    except ValueError:
        return 0


def extract_media_refs(total: int) -> tuple[list[dict], bool]:
    """Junta TODOS los adjuntos del mensaje, no sólo el primero.

    Devuelve (refs, truncado) — Twilio permite más adjuntos de los que conviene
    procesar en un request, así que se corta en MAX_MEDIA_PER_MESSAGE.
    """
    refs = []
    truncated = total > MAX_MEDIA_PER_MESSAGE
    for i in range(min(total, MAX_MEDIA_PER_MESSAGE)):
        url = request.values.get(f"MediaUrl{i}")
        if url:
            refs.append({"url": url, "mime": request.values.get(f"MediaContentType{i}")})
    return refs, truncated


def download_media(refs: list[dict]) -> list[dict]:
    """Descarga los adjuntos y los devuelve como [{"mime", "bytes"}].

    La URL de Twilio es directa pero pide auth básica con las credenciales de la
    cuenta (Telegram, en cambio, necesita resolver el file_id con getFile primero).
    """
    items = []
    for ref in refs:
        log.info("downloading media from %s", ref["url"])
        r = httpx.get(
            ref["url"],
            auth=(ACCOUNT_SID, AUTH_TOKEN),
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        r.raise_for_status()
        log.info("media downloaded: %d bytes", len(r.content))
        items.append({"mime": ref["mime"], "bytes": r.content})
    return items


def send_message(to: str, text: str) -> None:
    """Envía una respuesta por la API REST. No lanza: un fallo no debe cortar el batch."""
    if not enabled():
        log.error("canal Twilio apagado: no se pudo responder a %s", to)
        return
    try:
        _client.messages.create(
            from_=f"whatsapp:{WHATSAPP_NUMBER}",
            to=f"whatsapp:{to}",
            body=text,
        )
    except Exception:
        log.exception("no se pudo enviar la respuesta a %s", to)
