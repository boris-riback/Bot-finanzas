import base64
import io
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

import anthropic
import httpx
from flask import Flask, request
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

from bialystok_client import (
    confirm_pending,
    fetch_catalog,
    fetch_summary,
    ingest,
    list_pending,
    rrhh_advance,
    rrhh_confirm_liquidation,
    rrhh_liquidate,
    upload_attachment,
)

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MIMES = {"application/pdf"}
AUDIO_MIME_EXT = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/amr": "amr",
}

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

MAX_HISTORY = 10
conversation_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))


def add_to_history(phone: str, role: str, text: str):
    conversation_history[phone].append({
        "role": role,
        "text": text[:500],
        "ts": datetime.now().strftime("%H:%M"),
    })


def get_history_text(phone: str) -> str:
    history = conversation_history.get(phone)
    if not history:
        return ""
    lines = []
    for entry in history:
        prefix = "Usuario" if entry["role"] == "user" else "Bot"
        lines.append(f"[{entry['ts']}] {prefix}: {entry['text']}")
    return "\n".join(lines)


def is_audio_mime(mime: str | None) -> bool:
    return bool(mime) and mime.split(";")[0].strip().lower() in AUDIO_MIME_EXT


def transcribe_audio(audio_bytes: bytes, mime: str) -> str:
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY no configurada.")
    normalized = mime.split(";")[0].strip().lower()
    ext = AUDIO_MIME_EXT.get(normalized, "ogg")
    buf = io.BytesIO(audio_bytes)
    buf.name = f"voice.{ext}"
    resp = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=buf,
        language="es",
    )
    return (resp.text or "").strip()


def build_prompt_text(catalog: dict, body: str, has_attachment: bool, phone: str = "") -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    adjunto_block = ""
    if has_attachment:
        adjunto_block = (
            "\nSe adjunta un comprobante (PDF o imagen). Miralo y extraé:\n"
            "- monto total (amount)\n"
            "- fecha del comprobante (movementDate)\n"
            "- razón social / proveedor / contraparte\n"
            "- CUIT si está visible\n"
            "- CBU o alias bancario destino si aparece (counterpartyCbu)\n"
            "- tipo y número de comprobante (Factura A/B/C, Recibo, Ticket, Transferencia, etc.)\n"
            "- método de pago si está indicado\n"
        )

    body_instruction = ""
    if not body and has_attachment:
        body_instruction = (
            "\nEl usuario envió SOLO un comprobante sin texto. Inferí todo del documento:\n"
            "- Si es una factura de compra, recibo de pago a proveedor, o transferencia saliente → kind = \"egreso\".\n"
            "- Si es una factura de venta, cobro, o transferencia entrante → kind = \"ingreso\".\n"
            "- Extraé todos los datos posibles del comprobante.\n"
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
        f"{adjunto_block}"
        f"{body_instruction}\n"
        f"Fecha de hoy: {today}\n"
        f'Mensaje del usuario: "{body}"\n\n'
        "Devolvé JSON exacto con estos campos:\n"
        "{\n"
        '  "kind": "egreso" | "ingreso",\n'
        '  "classificationId": "<uuid del catálogo>",\n'
        '  "classificationName": "<nombre legible de la clasificación elegida>",\n'
        '  "conceptId": "<uuid del catálogo>",\n'
        '  "conceptName": "<nombre legible del concepto elegido>",\n'
        '  "movementTypeId": "<uuid>",\n'
        '  "movementTypeName": "<nombre legible del tipo de movimiento>",\n'
        '  "paymentMethodId": "<uuid>",\n'
        '  "paymentMethodName": "<nombre legible del método de pago>",\n'
        '  "counterpartyId": "<uuid si estás 100% seguro que matchea una counterparty del catálogo, si no null>",\n'
        '  "counterpartyName": "<nombre del proveedor que el usuario quiere asociar, null si no hay>",\n'
        '  "counterpartyAliasHints": ["otros nombres razón social vistos en el comprobante que no coinciden con counterpartyName"],\n'
        '  "counterpartyCbu": "<CBU o alias bancario del comprobante si aparece, null si no>",\n'
        '  "businessUnitId": "<uuid>",\n'
        '  "businessUnitName": "<nombre legible de la unidad de negocio>",\n'
        '  "amount": <number>,\n'
        '  "movementDate": "YYYY-MM-DD (hoy si no se menciona ni en texto ni en el adjunto)",\n'
        '  "status": "pendiente" | "pagado",\n'
        '  "receiptTypeId": "<uuid o null>",\n'
        '  "receiptNumber": "<string o null>",\n'
        '  "notes": "<observación libre del usuario, null si no hay>"\n'
        "}\n\n"
        "Reglas generales:\n"
        '- Si el mensaje menciona "pagado", "cobrado", "ya pagué", "pagué" → status = "pagado".\n'
        '- Si menciona "pendiente", "por pagar", "a pagar" → status = "pendiente".\n'
        '- Si no especifica estado → status = "pagado" (default).\n'
        "- counterpartyId: devolvé null salvo que el nombre coincida EXACTAMENTE con uno del catálogo. Si hay duda, null y poné el nombre crudo en counterpartyName.\n"
        '- Si el usuario dice "varios" o "proveedor varios" en el TEXTO, poné counterpartyName: "varios". Pero si "VARIOS" aparece solo en el comprobante como referencia/leyenda bancaria, NO es el proveedor — dejá counterpartyName en null.\n'
        '- Si no se menciona método de pago, usá "Efectivo".\n'
        "- NUNCA inventes UUIDs que no estén en el catálogo.\n"
        "- NUNCA inventes o adivines clasificación, concepto, o tipo de movimiento. Si el usuario o el comprobante no dicen explícitamente de qué rubro/categoría se trata, usá los valores más genéricos del catálogo (ej: la primera clasificación y concepto disponibles). NO intentes deducir el rubro a partir del tipo de comprobante.\n"
        "- notes: SOLO texto que el usuario escribió como observación libre. Disparadores: \"nota:\", \"obs:\", \"porque\", \"para\", \"es por\", \"corresponde a\", frases entre paréntesis.\n"
        "- notes NUNCA debe contener datos extraídos del comprobante: CUIT, razón social, detalle de items/artículos, CAE, número de cuenta, banco, CBU, domicilio, etc. Si el usuario no escribió ninguna observación, notes = null.\n"
        "- Si el usuario dice p.ej. \"egreso 5000 a test por el evento de junio\", notes debe ser \"por el evento de junio\" (NO inventes, solo copiá literal lo relevante).\n"
        '- Si algún campo obligatorio no puede inferirse, respondé con un objeto {"error": "motivo"} en lugar del JSON de movimiento.\n\n'
        "Reglas de lectura de comprobantes bancarios:\n"
        "- 'Trf Inmed Proveed' (Transferencia Inmediata a Proveedor) de Banco Galicia y similares:\n"
        "  * kind = 'egreso', paymentMethod = 'Transferencia', status = 'pagado'.\n"
        "  * 'Leyendas adicionales' tienen este orden: (1) nombre del DESTINATARIO, (2) CUIT del destinatario, (3) referencia/concepto libre, (4) banco.\n"
        "  * La primera leyenda es el nombre del destinatario → usalo como counterpartyName.\n"
        "  * La referencia (3ra leyenda, ej: 'VARIOS') NO es el proveedor, es solo un concepto de la transferencia. Ignorala para counterpartyName.\n"
        "  * El CUIT (2da leyenda) NO va en notes.\n"
        "- Para otros comprobantes bancarios sin formato conocido: si no hay dato claro del destinatario, counterpartyName = null.\n\n"
        "Reglas de resolución texto vs adjunto (cuando hay adjunto):\n"
        "- Monto (amount): gana el adjunto salvo que el texto diga explícitamente otro número.\n"
        "- Fecha: gana el adjunto salvo que el texto especifique otra.\n"
        "- Contraparte (counterpartyName): si el usuario nombra un proveedor en el texto, GANA el texto. Si el usuario no lo nombra, buscá razón social del DESTINATARIO en el adjunto. Si no hay destinatario claro, null.\n"
        "- counterpartyAliasHints: meté TODO nombre de razón social/titular visto en el adjunto que NO coincida con counterpartyName (para aprender alias). Si no hay diferencias, [].\n"
        "- CBU/alias: solo del adjunto, solo si corresponde al DESTINATARIO (no al emisor).\n"
        "- Método de pago y status: gana el texto del usuario.\n"
        "- Número de comprobante: del adjunto.\n"
        "- Si el adjunto es ilegible o no parece un comprobante, ignoralo y parseá solo el texto.\n\n"
        + (f"Historial reciente de la conversación:\n{get_history_text(phone)}\n\n" if phone and get_history_text(phone) else "")
        + "Reglas de contexto conversacional:\n"
        "- Si el usuario hace referencia a un mensaje anterior (\"ese\", \"el último\", \"el de recién\"), usá el historial para entender a qué se refiere.\n"
        "- Si dice \"fue pagado\", \"ya lo pagué\", \"pagalo\", \"ponelo como pagado\" sin especificar monto ni proveedor, está corrigiendo el último movimiento. Devolvé el JSON con los mismos datos del último movimiento registrado (visible en el historial) pero con status = \"pagado\".\n"
        "- Si dice \"cambiale el proveedor a X\" o \"era para proveedor X\", devolvé el JSON corrigiendo solo el campo mencionado.\n"
        "- Si el mensaje es claramente un movimiento nuevo (tiene monto, proveedor, etc.), ignorá el historial y parseá normalmente.\n\n"
        "Respondé SOLO el JSON, sin markdown ni explicación."
    )


def claude_parse(body: str, catalog: dict, media_bytes: bytes | None, media_mime: str | None, phone: str = "") -> dict:
    content_blocks: list = []

    if media_bytes and media_mime:
        b64 = base64.b64encode(media_bytes).decode()
        if media_mime in PDF_MIMES:
            content_blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            })
        elif media_mime in IMAGE_MIMES:
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_mime, "data": b64},
            })

    has_attachment = bool(content_blocks)
    content_blocks.append({
        "type": "text",
        "text": build_prompt_text(catalog, body, has_attachment=has_attachment, phone=phone),
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
        "Comandos:\n"
        "- /resumen: pagos pendientes próximos 7 y 30 días\n"
        "- /adelanto <nombre> <monto> [nota]: registra adelanto a empleado\n"
        "- /liquidar <nombre>: prepara liquidación semanal (pide SI/NO)\n"
        "- /cancelar: cancela selección pendiente\n\n"
        "Mandá texto libre con foto/PDF/audio para registrar movimiento. "
        "Si aparecen opciones de proveedor, respondé con el número."
    ),
}

# Commands that need dynamic handling (not static responses)
DYNAMIC_COMMANDS = {"/resumen"}


PENDING_LIQUIDATION_TTL = 1800
pending_liquidations: dict[str, dict] = {}

LIQ_CONFIRM_WORDS = {"si", "sí", "ok", "confirmar", "dale"}
LIQ_CANCEL_WORDS = {"no", "cancelar"}


def set_pending_liquidation(phone: str, pending_id: str):
    pending_liquidations[phone] = {
        "pendingId": pending_id,
        "expiresAt": time.time() + PENDING_LIQUIDATION_TTL,
    }


def get_pending_liquidation(phone: str) -> dict | None:
    entry = pending_liquidations.get(phone)
    if not entry:
        return None
    if time.time() > entry["expiresAt"]:
        pending_liquidations.pop(phone, None)
        return None
    return entry


def clear_pending_liquidation(phone: str):
    pending_liquidations.pop(phone, None)


ADELANTO_RE = re.compile(r"^/adelanto\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
LIQUIDAR_RE = re.compile(r"^/liquidar\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
AMOUNT_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def parse_adelanto(body: str) -> dict | None:
    m = ADELANTO_RE.match(body.strip())
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest:
        return None
    num = AMOUNT_RE.search(rest)
    if not num:
        return None
    name = rest[: num.start()].strip().rstrip(",")
    if not name:
        return None
    try:
        amount = float(num.group(0).replace(",", "."))
    except ValueError:
        return None
    note = rest[num.end():].strip() or None
    return {"name": name, "amount": amount, "note": note}


def parse_liquidar(body: str) -> str | None:
    m = LIQUIDAR_RE.match(body.strip())
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def _extract_edge_error(e: httpx.HTTPStatusError) -> tuple[str, str]:
    try:
        body = e.response.json()
    except Exception:
        return "", ""
    return body.get("error", "") or "", body.get("message", "") or ""


def _map_rrhh_error(code: str, msg: str, employee_name: str = "") -> str | None:
    if code == "duplicate_origin_ref":
        return None
    if code == "employee_not_found":
        ref = employee_name or "ese empleado"
        return f"No encontré a {ref}. Revisá el nombre o escribilo más completo."
    if code == "employee_ambiguous":
        return "Hay varios empleados con ese nombre. Sé más específico."
    if code == "payroll_mode_not_supported":
        return "Ese empleado no está en modalidad semanal, no puedo liquidarlo por acá."
    if code in ("pending_not_found", "pending_expired"):
        return "No hay liquidación pendiente o ya expiró. Mandá /liquidar <nombre> de nuevo."
    return msg or "Hubo un error procesando tu pedido."


def handle_adelanto(phone: str, sid: str, body: str) -> str:
    parsed = parse_adelanto(body)
    if not parsed:
        return "Formato: /adelanto <nombre> <monto> [nota]"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        result = rrhh_advance(
            phone=phone,
            origin_ref=f"wa:{sid}",
            employee_name=parsed["name"],
            amount=parsed["amount"],
            date=today,
            note=parsed["note"],
        )
    except httpx.HTTPStatusError as e:
        code, msg = _extract_edge_error(e)
        mapped = _map_rrhh_error(code, msg, parsed["name"])
        return mapped or ""
    return result.get("message") or "✅ Adelanto registrado."


def handle_liquidar(phone: str, sid: str, body: str) -> str:
    name = parse_liquidar(body)
    if not name:
        return "Formato: /liquidar <nombre completo>"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        result = rrhh_liquidate(
            phone=phone,
            origin_ref=f"wa:{sid}",
            employee_name=name,
            reference_date=today,
        )
    except httpx.HTTPStatusError as e:
        code, msg = _extract_edge_error(e)
        mapped = _map_rrhh_error(code, msg, name)
        return mapped or ""
    pending_id = result.get("pendingId")
    if pending_id:
        set_pending_liquidation(phone, pending_id)
    return result.get("message") or "Liquidación preparada. Respondé SI para confirmar o NO para cancelar."


def handle_liquidation_confirmation(phone: str, body: str) -> str | None:
    """Return reply text if consumed, None to let message follow normal flow."""
    entry = get_pending_liquidation(phone)
    if not entry:
        return None
    lower = (body or "").strip().lower()
    if lower in LIQ_CONFIRM_WORDS:
        confirm = True
    elif lower in LIQ_CANCEL_WORDS:
        confirm = False
    else:
        clear_pending_liquidation(phone)
        return None
    try:
        result = rrhh_confirm_liquidation(phone, entry["pendingId"], confirm)
    except httpx.HTTPStatusError as e:
        clear_pending_liquidation(phone)
        code, msg = _extract_edge_error(e)
        if code == "duplicate_origin_ref":
            return ""
        return _map_rrhh_error(code, msg) or ""
    clear_pending_liquidation(phone)
    default = "✅ Liquidación confirmada." if confirm else "Liquidación cancelada."
    return result.get("message") or default


def format_summary_section(title: str, items: list, total: float) -> str:
    if not items:
        return f"{title}: sin pendientes"
    lines = [f"{title} ({len(items)} pagos, {format_amount(total)}):"]
    for m in items[:10]:
        cp = m.get("counterparty_name") or "Sin proveedor"
        lines.append(f"  • {format_amount(m.get('amount', 0))} → {cp} ({m.get('movement_date', '')})")
    if len(items) > 10:
        lines.append(f"  ... y {len(items) - 10} más")
    return "\n".join(lines)


def handle_summary(phone: str) -> str:
    data = fetch_summary(phone)
    sections = []
    overdue = data.get("overdue", {})
    if overdue.get("count", 0) > 0:
        sections.append(format_summary_section(
            "⚠️ *VENCIDOS*", overdue.get("items", []), overdue.get("total", 0)))
    sections.append(format_summary_section(
        "📅 *Próximos 7 días*", data.get("next7", {}).get("items", []), data.get("next7", {}).get("total", 0)))
    sections.append(format_summary_section(
        "📅 *Próximos 30 días*", data.get("next30", {}).get("items", []), data.get("next30", {}).get("total", 0)))
    return "\n\n".join(sections)


def is_command(text: str) -> str | None:
    t = text.strip().lower()
    if t in COMMAND_RESPONSES or t in DYNAMIC_COMMANDS:
        return t
    return None


def twilio_reply(text: str) -> str:
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)


def send_whatsapp(to: str, text: str):
    """Send a WhatsApp message via Twilio REST API (for async replies)."""
    twilio_client.messages.create(
        from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
        to=f"whatsapp:{to}",
        body=text,
    )


def format_amount(value) -> str:
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def extract_receipt_info(result: dict | None) -> dict | None:
    """Detect backend-generated receipt in ingest/confirm response.

    Returns {"id", "number", "url"} when present, else None.
    Shape: either result["receipt"] = {...} or result["receiptGenerated"] = True.
    """
    if not isinstance(result, dict):
        return None
    receipt = result.get("receipt")
    if isinstance(receipt, dict):
        return {
            "id": receipt.get("id"),
            "number": receipt.get("number"),
            "url": receipt.get("url") or receipt.get("pdfUrl"),
        }
    if result.get("receiptGenerated"):
        return {"id": None, "number": None, "url": None}
    return None


def format_receipt_line(result: dict | None) -> str:
    info = extract_receipt_info(result)
    if not info:
        return ""
    number = info.get("number")
    if number:
        return f"🧾 Comprobante #{number} generado"
    return "🧾 Comprobante generado"


def log_receipt(result: dict | None):
    info = extract_receipt_info(result)
    if info:
        log.info("receipt generated | id=%s number=%s", info.get("id"), info.get("number"))
    else:
        log.info("receipt not generated")


def format_movement_reply(movement: dict, parsed_fallback: dict | None = None) -> str:
    fb = parsed_fallback or {}

    def _get(key, fb_key=None):
        return movement.get(key) or fb.get(fb_key or key) or ""

    kind = _get("kind")
    amount = movement.get("amount", fb.get("amount", 0))
    status = _get("status")
    cp = _get("counterparty_name", "counterpartyName")
    notes = _get("notes")
    date = _get("movement_date", "movementDate")
    classification = _get("classification_name", "classificationName")
    concept = _get("concept_name", "conceptName")
    payment_method = _get("payment_method_name", "paymentMethodName")
    business_unit = _get("business_unit_name", "businessUnitName")
    receipt_number = _get("receipt_number", "receiptNumber")

    emoji = "💸" if kind == "egreso" else "💰"
    lines = [f"{emoji} *{kind.upper()}* {format_amount(amount)}"]
    if cp:
        lines.append(f"👤 {cp}")
    if payment_method:
        lines.append(f"💳 {payment_method}")
    if date:
        lines.append(f"📅 {date}")
    if notes:
        lines.append(f"📝 {notes}")
    receipt_line = format_receipt_line(movement)
    if receipt_line:
        lines.append(receipt_line)
    return "\n".join(lines)


def format_duplicate_reply(result: dict | None) -> str:
    """Reply for a deduped ingest: header + movement details (+ receipt line if present)."""
    header = "⚠️ Movimiento ya registrado previamente."
    if not isinstance(result, dict):
        return header
    has_content = any(
        result.get(k) for k in ("kind", "amount", "counterparty_name", "counterpartyName")
    )
    if not has_content:
        return header
    return f"{header}\n{format_movement_reply(result)}"


def format_candidate_menu(candidates: list, suggested_name: str, suggested_cbu: str) -> str:
    lines = []
    if suggested_cbu:
        lines.append(f'No reconocí el proveedor de "{suggested_name or suggested_cbu}".')
    elif suggested_name:
        lines.append(f'No reconocí al proveedor "{suggested_name}".')
    else:
        lines.append("No encontré proveedor.")
    lines.append("")
    lines.append("¿Cuál es? Respondé con el número:")
    for i, c in enumerate(candidates[:5], start=1):
        lines.append(f"{i}. {c.get('name')}")
    next_idx = min(len(candidates), 5) + 1
    lines.append(f"{next_idx}. Proveedor Varios")
    next_idx += 1
    if suggested_name:
        lines.append(f"{next_idx}. Crear nuevo: {suggested_name}")
        next_idx += 1
    lines.append(f"{next_idx}. Cancelar")
    return "\n".join(lines)


NUMERIC_REPLY = re.compile(r"^\s*(\d+)\s*$")


def try_parse_numeric(body: str) -> int | None:
    m = NUMERIC_REPLY.match(body or "")
    return int(m.group(1)) if m else None


def handle_pending_reply(phone: str, body: str) -> str | None:
    """Return twilio reply string if this message resolves a pending selection; else None."""
    lower = (body or "").strip().lower()
    if lower == "/cancelar":
        resp = confirm_pending(phone, {"kind": "cancel"})
        if resp.get("cancelled"):
            return twilio_reply("Selección cancelada.")
        return None

    choice_num = try_parse_numeric(body)
    if choice_num is None:
        return None

    pending_resp = list_pending(phone)
    pending = pending_resp.get("pending")
    if not pending:
        return None

    candidates = pending.get("candidates") or []
    raw_name = pending.get("raw_counterparty_name") or ""
    visible = candidates[:5]
    next_idx = len(visible) + 1
    varios_idx = next_idx
    next_idx += 1
    create_idx = next_idx if raw_name else None
    if raw_name:
        next_idx += 1
    cancel_idx = next_idx

    if 1 <= choice_num <= len(visible):
        cp = visible[choice_num - 1]
        result = confirm_pending(
            phone,
            {"kind": "existing", "counterpartyId": cp["id"]},
            pending_id=pending["id"],
        )
    elif choice_num == varios_idx:
        result = confirm_pending(
            phone,
            {"kind": "varios"},
            pending_id=pending["id"],
        )
    elif create_idx and choice_num == create_idx:
        result = confirm_pending(
            phone,
            {"kind": "new", "name": raw_name},
            pending_id=pending["id"],
        )
    elif choice_num == cancel_idx:
        result = confirm_pending(phone, {"kind": "cancel"}, pending_id=pending["id"])
        if result.get("cancelled"):
            return twilio_reply("Selección cancelada.")
        return twilio_reply("No pude cancelar.")
    else:
        return twilio_reply("Opción inválida. Respondé con un número del menú o /cancelar.")

    if result.get("duplicated"):
        log_receipt(result)
        return twilio_reply(format_duplicate_reply(result))
    log_receipt(result)
    return twilio_reply(format_movement_reply(result))


def process_message(phone: str, body: str, sid: str, media_bytes: bytes | None, media_mime: str | None):
    """Core processing logic. Returns reply text string."""
    log.info("process start | phone=%s body=%r mime=%s has_media=%s", phone, body[:80], media_mime, bool(media_bytes))

    if media_bytes and is_audio_mime(media_mime):
        log.info("transcribing audio (%d bytes, %s)", len(media_bytes), media_mime)
        transcript = transcribe_audio(media_bytes, media_mime)
        log.info("transcript: %r", transcript[:200] if transcript else "")
        if not transcript:
            return "No pude transcribir el audio. Mandalo otra vez o escribilo."
        pending_reply_text = _check_pending(phone, transcript)
        if pending_reply_text:
            return pending_reply_text
        body = f"{body} {transcript}".strip() if body else transcript
        media_bytes = None
        media_mime = None

    log.info("fetching catalog")
    catalog = fetch_catalog(phone)
    add_to_history(phone, "user", body)

    log.info("calling claude_parse body=%r", body[:80])
    parsed = claude_parse(body, catalog, media_bytes, media_mime, phone=phone)
    log.info("parsed: %s", json.dumps(parsed, ensure_ascii=False)[:300])

    if "error" in parsed:
        error_reply = f"No pude interpretar: {parsed['error']}"
        add_to_history(phone, "bot", error_reply)
        return error_reply

    attachment_path = None
    if media_bytes:
        log.info("uploading attachment")
        attachment_path = upload_attachment(phone, sid, media_mime, media_bytes)

    log.info("calling ingest")
    result = ingest({
        "phone": phone,
        "originRef": sid,
        "attachmentPath": attachment_path,
        **parsed,
    })
    log.info("ingest result: %s", json.dumps(result, ensure_ascii=False)[:300])
    log_receipt(result)

    if result.get("status") == "needs_selection":
        menu = format_candidate_menu(
            result.get("candidates") or [],
            result.get("suggestedName") or "",
            result.get("suggestedCbu") or "",
        )
        add_to_history(phone, "bot", menu)
        return menu

    if result.get("duplicated"):
        dup_reply = format_duplicate_reply(result)
        add_to_history(phone, "bot", dup_reply)
        return dup_reply

    reply = format_movement_reply(result, parsed)
    add_to_history(phone, "bot", reply)
    return reply


def _check_pending(phone: str, text: str) -> str | None:
    """Check if text resolves a pending selection. Returns reply text or None."""
    lower = text.strip().lower()
    if lower == "/cancelar":
        resp = confirm_pending(phone, {"kind": "cancel"})
        if resp.get("cancelled"):
            return "Selección cancelada."
        return None

    choice_num = try_parse_numeric(text)
    if choice_num is None:
        return None

    pending_resp = list_pending(phone)
    pending = pending_resp.get("pending")
    if not pending:
        return None

    candidates = pending.get("candidates") or []
    raw_name = pending.get("raw_counterparty_name") or ""
    visible = candidates[:5]
    next_idx = len(visible) + 1
    varios_idx = next_idx
    next_idx += 1
    create_idx = next_idx if raw_name else None
    if raw_name:
        next_idx += 1
    cancel_idx = next_idx

    if 1 <= choice_num <= len(visible):
        cp = visible[choice_num - 1]
        result = confirm_pending(phone, {"kind": "existing", "counterpartyId": cp["id"]}, pending_id=pending["id"])
    elif choice_num == varios_idx:
        result = confirm_pending(phone, {"kind": "varios"}, pending_id=pending["id"])
    elif create_idx and choice_num == create_idx:
        result = confirm_pending(phone, {"kind": "new", "name": raw_name}, pending_id=pending["id"])
    elif choice_num == cancel_idx:
        result = confirm_pending(phone, {"kind": "cancel"}, pending_id=pending["id"])
        if result.get("cancelled"):
            return "Selección cancelada."
        return "No pude cancelar."
    else:
        return "Opción inválida. Respondé con un número del menú o /cancelar."

    if result.get("duplicated"):
        log_receipt(result)
        return format_duplicate_reply(result)
    log_receipt(result)
    return format_movement_reply(result)


def process_async(phone, body, sid, media_bytes, media_mime):
    """Run processing in background thread, send reply via Twilio API."""
    try:
        reply_text = process_message(phone, body, sid, media_bytes, media_mime)
        send_whatsapp(phone, reply_text)
        log.info("async reply sent to %s", phone)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 403:
            send_whatsapp(phone, "Número no autorizado.")
            return
        try:
            detail = e.response.json().get("error") or e.response.text
        except Exception:
            detail = e.response.text
        send_whatsapp(phone, f"Error ingest ({code}): {str(detail)[:300]}")
    except json.JSONDecodeError:
        send_whatsapp(phone, "Error interpretando el mensaje. Intentá de nuevo.")
    except Exception as e:
        log.exception("async processing error")
        send_whatsapp(phone, f"Error: {str(e)[:400]}")


@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.values.get("From", "").replace("whatsapp:", "")
    body = (request.values.get("Body") or "").strip()
    sid = request.values.get("MessageSid", "")
    num_media = int(request.values.get("NumMedia", 0) or 0)
    media_url = request.values.get("MediaUrl0") if num_media > 0 else None
    media_mime = request.values.get("MediaContentType0") if num_media > 0 else None

    log.info("webhook | phone=%s body=%r num_media=%d mime=%s", phone, body[:80] if body else "", num_media, media_mime)

    try:
        # Quick sync replies: pending selection or commands (no heavy processing)
        if not media_url and body:
            # Liquidation confirmation (si/no/ok/cancelar) if pendingId active
            liq_reply = handle_liquidation_confirmation(phone, body)
            if liq_reply is not None:
                if liq_reply == "":
                    return "", 200
                return twilio_reply(liq_reply)

            lower_body = body.strip().lower()
            if lower_body.startswith("/adelanto"):
                reply = handle_adelanto(phone, sid, body)
                if not reply:
                    return "", 200
                return twilio_reply(reply)
            if lower_body.startswith("/liquidar"):
                reply = handle_liquidar(phone, sid, body)
                if not reply:
                    return "", 200
                return twilio_reply(reply)

            pending_reply = handle_pending_reply(phone, body)
            if pending_reply is not None:
                return pending_reply

            cmd = is_command(body)
            if cmd:
                if cmd in COMMAND_RESPONSES:
                    return twilio_reply(COMMAND_RESPONSES[cmd])
                if cmd == "/resumen":
                    return twilio_reply(handle_summary(phone))

        if not body and not media_url:
            return twilio_reply("Mensaje vacío. Escribí /ayuda.")

        # Download media if present
        media_bytes = None
        if media_url:
            log.info("downloading media from %s", media_url)
            r = httpx.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=30, follow_redirects=True)
            r.raise_for_status()
            media_bytes = r.content
            log.info("media downloaded: %d bytes", len(media_bytes))

        # Heavy processing → async (audio transcription, vision, etc.)
        # Keeps Twilio from timing out; reply sent via REST API
        needs_async = (media_bytes and is_audio_mime(media_mime)) or media_bytes is not None
        if needs_async:
            log.info("heavy processing detected (audio=%s, media=%s), going async",
                     is_audio_mime(media_mime) if media_mime else False, bool(media_bytes))
            threading.Thread(
                target=process_async,
                args=(phone, body, sid, media_bytes, media_mime),
                daemon=True,
            ).start()
            return "", 200

        # Text-only — process synchronously
        reply_text = process_message(phone, body, sid, media_bytes, media_mime)
        return twilio_reply(reply_text)

    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        log.error("HTTP error %d: %s", code, e.response.text[:300])
        if code == 403:
            return twilio_reply("Número no autorizado.")
        try:
            detail = e.response.json().get("error") or e.response.text
        except Exception:
            detail = e.response.text
        return twilio_reply(f"Error ingest ({code}): {str(detail)[:300]}")
    except json.JSONDecodeError:
        log.exception("JSON decode error")
        return twilio_reply("Error interpretando el mensaje. Intentá de nuevo.")
    except Exception as e:
        log.exception("webhook error")
        return twilio_reply(f"Error: {str(e)[:400]}")


@app.route("/", methods=["GET"])
def health():
    return "Bot Finanzas OK", 200


if __name__ == "__main__":
    app.run(debug=False, port=5000)
