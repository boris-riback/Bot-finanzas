import base64
import json
import os
import re
from datetime import datetime

import anthropic
import httpx
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from bialystok_client import fetch_catalog, ingest, upload_attachment

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MIMES = {"application/pdf"}

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def build_prompt_text(catalog: dict, body: str, has_attachment: bool) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    adjunto_block = ""
    if has_attachment:
        adjunto_block = (
            "\nSe adjunta un comprobante (PDF o imagen). Mirá el documento y extraé:\n"
            "- monto total (amount)\n"
            "- fecha del comprobante (movementDate)\n"
            "- razón social / proveedor / contraparte\n"
            "- CUIT si está visible\n"
            "- tipo y número de comprobante (Factura A/B/C, Recibo, Ticket, Transferencia, etc.)\n"
            "- método de pago si está indicado\n"
        )

    return (
        "Sos un parser de movimientos financieros para un ERP.\n\n"
        "Catálogo disponible para esta organización:\n"
        f"- businessUnits: {json.dumps(catalog.get('businessUnits', []), ensure_ascii=False)}\n"
        f"- classifications: {json.dumps(catalog.get('classifications', []), ensure_ascii=False)}\n"
        f"- concepts: {json.dumps(catalog.get('concepts', []), ensure_ascii=False)}\n"
        f"- movementTypes: {json.dumps(catalog.get('movementTypes', []), ensure_ascii=False)}\n"
        f"- paymentMethods: {json.dumps(catalog.get('paymentMethods', []), ensure_ascii=False)}\n"
        f"- counterparties: {json.dumps(catalog.get('counterparties', []), ensure_ascii=False)}\n"
        f"- receiptTypes: {json.dumps(catalog.get('receiptTypes', []), ensure_ascii=False)}\n"
        f"{adjunto_block}\n"
        f"Fecha de hoy: {today}\n"
        f'Mensaje del usuario: "{body}"\n\n'
        "Devolvé JSON exacto con estos campos:\n"
        "{\n"
        '  "kind": "egreso" | "ingreso",\n'
        '  "classificationId": "<uuid del catálogo>",\n'
        '  "conceptId": "<uuid del catálogo>",\n'
        '  "movementTypeId": "<uuid>",\n'
        '  "paymentMethodId": "<uuid>",\n'
        '  "counterpartyId": "<uuid o null>",\n'
        '  "counterpartyName": "<string si la contraparte no está en el catálogo, null si ya viene counterpartyId>",\n'
        '  "businessUnitId": "<uuid>",\n'
        '  "amount": <number>,\n'
        '  "movementDate": "YYYY-MM-DD (hoy si no se menciona ni en texto ni en el adjunto)",\n'
        '  "status": "pendiente" | "pagado",\n'
        '  "receiptTypeId": "<uuid o null>",\n'
        '  "receiptNumber": "<string o null>",\n'
        '  "notes": "<string o null — incluí CUIT acá si lo viste>"\n'
        "}\n\n"
        "Reglas generales:\n"
        '- Si el mensaje menciona "pagado", "cobrado", "ya pagué", "pagué" → status = "pagado".\n'
        '- Si menciona "pendiente", "por pagar", "a pagar", o no especifica → status = "pendiente".\n'
        "- Si la contraparte no existe en counterparties, devolvé counterpartyId: null y counterpartyName con el texto.\n"
        '- Si no se menciona método de pago, usá "Efectivo".\n'
        "- NUNCA inventes UUIDs que no estén en el catálogo.\n"
        '- Si algún campo obligatorio no puede inferirse, respondé con un objeto {"error": "motivo"} en lugar del JSON de movimiento.\n\n'
        "Reglas de resolución texto vs adjunto (cuando hay adjunto):\n"
        "- Monto (amount): gana el adjunto salvo que el texto diga explícitamente otro número.\n"
        "- Fecha: gana el adjunto salvo que el texto especifique otra.\n"
        "- Contraparte / razón social: gana el adjunto.\n"
        "- Método de pago y status: gana el texto del usuario.\n"
        "- Número de comprobante: del adjunto.\n"
        "- Si el adjunto es ilegible o no parece un comprobante, ignoralo y parseá solo el texto.\n\n"
        "Respondé SOLO el JSON, sin markdown ni explicación."
    )


def claude_parse(body: str, catalog: dict, media_bytes: bytes | None, media_mime: str | None) -> dict:
    content_blocks: list = []

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

    has_attachment = bool(content_blocks)
    content_blocks.append({
        "type": "text",
        "text": build_prompt_text(catalog, body, has_attachment=has_attachment),
    })

    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": content_blocks}],
    )

    text = resp.content[0].text
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)


COMMAND_RESPONSES = {
    "/ayuda": (
        "Comandos disponibles:\n"
        "- /saldo: Saldo actual de cajas\n"
        "- /reporte: Resumen del mes\n"
        "- /pendientes: Cheques y facturas por vencer\n\n"
        "Para registrar un movimiento mandá texto libre, opcionalmente con foto/PDF del comprobante."
    ),
    "/saldo": "Saldo: próximamente (migración a ERP en curso).",
    "/reporte": "Reporte: próximamente (migración a ERP en curso).",
    "/pendientes": "Pendientes: próximamente (migración a ERP en curso).",
}


def is_command(text: str) -> str | None:
    t = text.strip().lower()
    if t in COMMAND_RESPONSES:
        return t
    return None


def twilio_reply(text: str) -> str:
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)


@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.values.get("From", "").replace("whatsapp:", "")
    body = (request.values.get("Body") or "").strip()
    sid = request.values.get("MessageSid", "")
    num_media = int(request.values.get("NumMedia", 0) or 0)
    media_url = request.values.get("MediaUrl0") if num_media > 0 else None
    media_mime = request.values.get("MediaContentType0") if num_media > 0 else None

    try:
        cmd = is_command(body) if body and not media_url else None
        if cmd:
            return twilio_reply(COMMAND_RESPONSES[cmd])

        if not body and not media_url:
            return twilio_reply("Mensaje vacío. Escribí /ayuda.")

        media_bytes = None
        if media_url:
            r = httpx.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30)
            r.raise_for_status()
            media_bytes = r.content

        catalog = fetch_catalog(phone)
        parsed = claude_parse(body, catalog, media_bytes, media_mime)

        if "error" in parsed:
            return twilio_reply(f"No pude interpretar: {parsed['error']}")

        attachment_path = None
        if media_bytes:
            attachment_path = upload_attachment(phone, sid, media_mime, media_bytes)

        result = ingest({
            "phone": phone,
            "originRef": sid,
            "attachmentPath": attachment_path,
            **parsed,
        })

        if result.get("duplicated"):
            return twilio_reply("Movimiento ya registrado previamente.")

        amount = result.get("amount", parsed.get("amount", 0))
        status = result.get("status", parsed.get("status", ""))
        emoji = "💸" if parsed.get("kind") == "egreso" else "💰"
        return twilio_reply(f"{emoji} {parsed['kind']} ${float(amount):,.0f} ({status})")

    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 403:
            return twilio_reply("Número no autorizado.")
        return twilio_reply(f"Error ingest ({code}).")
    except json.JSONDecodeError:
        return twilio_reply("Error interpretando el mensaje. Intentá de nuevo.")
    except Exception as e:
        return twilio_reply(f"Error: {str(e)[:400]}")


@app.route("/", methods=["GET"])
def health():
    return "Bot Finanzas OK", 200


if __name__ == "__main__":
    app.run(debug=False, port=5000)
