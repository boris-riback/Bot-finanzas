import base64
import io
import json
import logging
import os
import re
import threading
from collections import defaultdict, deque
from datetime import datetime

import anthropic
import httpx
from flask import Flask, request
from openai import OpenAI

import telegram_api
import twilio_api
from bialystok_client import (
    confirm_cancel_movement,
    confirm_comprobante_pending,
    confirm_pending,
    confirm_transfer_pending,
    fetch_catalog,
    fetch_summary,
    ingest,
    internal_transfer,
    list_pending,
    list_receipts,
    receipt_pdf,
    request_cancel_movement,
    rrhh_advance,
    rrhh_confirm_liquidation,
    rrhh_liquidate,
    upload_attachment,
)

app = Flask(__name__)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Los errores del backend se loguean siempre; esto sólo controla si además se
# le muestran al usuario en el chat.
EXPOSE_ERROR_DETAIL = _env_flag("EXPOSE_ERROR_DETAIL", False)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MIMES = {"application/pdf"}

# Espejo del marcador que usa la app web (src/shared/lib/treasuryChecks.js)
# para serializar el numero de cheque dentro de movements.notes. El backend
# (edge function whatsapp-bot-ingest) lo embebe al recibir chequeNumber;
# aca lo parseamos al armar la respuesta para mostrar el numero limpio.
CHEQUE_NOTE_MARKER = "[[APP_BIALYSTOK_CHEQUE:"
CHEQUE_NOTE_SUFFIX = "]]"


def parse_movement_notes_with_cheque(notes: str | None) -> tuple[str, str]:
    """Devuelve (cheque_number, notes_clean) del campo notes."""
    raw = (notes or "")
    stripped = raw.lstrip()
    if not stripped.startswith(CHEQUE_NOTE_MARKER):
        return "", raw.strip()
    marker_end = stripped.find(CHEQUE_NOTE_SUFFIX)
    if marker_end == -1:
        return "", raw.strip()
    cheque = stripped[len(CHEQUE_NOTE_MARKER):marker_end].strip()
    rest = stripped[marker_end + len(CHEQUE_NOTE_SUFFIX):].lstrip()
    return cheque, rest.strip()
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
            "- fecha de vencimiento / pago futuro (dueDate) — típico en cheques electrónicos (eCheq), cheques comunes, facturas con plazo de pago\n"
            "- razón social / proveedor / contraparte\n"
            "- CUIT si está visible\n"
            "- CBU o alias bancario destino si aparece (counterpartyCbu)\n"
            "- tipo y número de comprobante (Factura A/B/C, Recibo, Ticket, Transferencia, etc.)\n"
            "- número de cheque / eCheq si aparece (chequeNumber)\n"
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
        f"- cashBoxes: {json.dumps(catalog.get('cashBoxes', []), ensure_ascii=False)}\n"
        f"{adjunto_block}"
        f"{body_instruction}\n"
        f"Fecha de hoy: {today}\n"
        f'Mensaje del usuario: "{body}"\n\n'
        "Decidí primero el TIPO de operación:\n"
        "- \"egreso\" | \"ingreso\": pago/cobro real (plata entró o salió, o queda como pendiente contra una contraparte).\n"
        "- \"transferencia\": movimiento interno entre dos cajas/cuentas propias. Disparadores: \"transferí X de [caja1] a [caja2]\", \"moví/pasé X de [caja1] a [caja2]\", \"transferencia interna\". NO hay contraparte.\n"
        "- \"recibo\": el usuario PIDE un recibo ya emitido, no carga nada. Disparadores: \"pasame el recibo de X\", \"mandame el último recibo de X\", \"quiero el recibo RP-000012\".\n"
        "  Devolvé el texto de búsqueda en \"search\": el nombre de la contraparte o el número de recibo, sin palabras de relleno.\n"
        "  \"pasame el ultimo recibo de mgb\" → search \"mgb\". \"mandame el RP-000012\" → search \"RP-000012\". Si no menciona ninguno, search vacío.\n"
        "- \"anular\": el usuario pide DESHACER lo último que cargó. Disparadores: \"borrá/eliminá/anulá el último movimiento\", \"deshacé lo último\", \"cancelá lo que cargué recién\", \"me equivoqué, sacá eso\".\n"
        "  Sólo aplica si NO hay datos de un movimiento nuevo en el mensaje. Si el usuario dice \"borrá el último y cargá X\", priorizá cargar X.\n"
        "  Si tenés dudas entre anular y cargar, elegí cargar: anular se confirma después y cargar de más es más facil de revertir que anular de más.\n\n"
        "Si es anular, devolvé JSON:\n"
        "{ \"kind\": \"anular\" }\n\n"
        "Si es recibo, devolvé JSON:\n"
        "{ \"kind\": \"recibo\", \"search\": \"<contraparte o numero, vacio si no menciona>\" }\n\n"
        "Si es transferencia, devolvé JSON:\n"
        "{\n"
        '  "kind": "transferencia",\n'
        '  "fromCashBoxName": "<nombre de la caja origen, como lo escribió el usuario o matcheado al catálogo cashBoxes>",\n'
        '  "toCashBoxName": "<nombre de la caja destino>",\n'
        '  "amount": <number>,\n'
        '  "movementDate": "YYYY-MM-DD (hoy si no se menciona)",\n'
        '  "notes": "<texto libre extra del usuario, null si no hay>"\n'
        "}\n\n"
        "Si es egreso o ingreso, devolvé JSON con estos campos:\n"
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
        '  "dueDate": "<YYYY-MM-DD fecha de vencimiento si aparece (eCheq, cheque, factura con plazo). null si no hay.>",\n'
        '  "status": "pendiente" | "pagado",\n'
        '  "receiptTypeId": "<uuid o null>",\n'
        '  "receiptNumber": "<string o null>",\n'
        '  "chequeNumber": "<string o null — solo si el adjunto es un cheque/eCheq>",\n'
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
        "- Cheque electrónico (eCheq) o cheque común:\n"
        "  * paymentMethod debe matchear el catálogo (variantes: 'Cheque', 'eCheq', 'Cheque Electrónico'). Si no hay match, usá el más cercano del catálogo paymentMethods.\n"
        "  * dueDate = fecha de PAGO/VENCIMIENTO del cheque (la fecha en que se cobra). NO uses la fecha de emisión acá — esa va en movementDate.\n"
        "  * Si el cheque es al día y solo hay una fecha visible, dueDate = movementDate.\n"
        "  * status = 'pendiente' si el cheque está a fecha futura respecto a hoy, 'pagado' si ya venció o fue depositado.\n"
        "  * chequeNumber = número de cheque / eCheq tal como aparece en el adjunto. En un eCheq suele estar como 'Número de cheque', 'N°' o 'Nro', generalmente 7-8 dígitos. En un cheque físico está impreso arriba a la derecha. Solo dígitos, sin guiones ni espacios. Si no aparece, null.\n"
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


# Ayuda por temas: el índice entra de un vistazo y el detalle se pide aparte.
# Texto plano a propósito — send_message no manda parse_mode, así que cualquier
# marca de Markdown se vería literal en el chat.
HELP_INDEX = (
    "🤖 Bot Finanzas — qué sé hacer\n"
    "\n"
    "Escribime en lenguaje natural. No hace falta formato.\n"
    "Ej: \"MGB 5000 efectivo\" · \"ingreso 120000 alquiler chopera\"\n"
    "\n"
    "Temas (pedí el detalle con /ayuda <tema>):\n"
    "  /ayuda cargar          registrar egresos e ingresos\n"
    "  /ayuda adjuntos        fotos, PDF y audios\n"
    "  /ayuda proveedores     cuando no reconozco a quién le pagaste\n"
    "  /ayuda corregir        arreglar lo último que cargaste\n"
    "  /ayuda transferencias  mover plata entre tus cajas\n"
    "  /ayuda rrhh            adelantos y liquidaciones\n"
    "  /ayuda consultas       qué le podés preguntar\n"
    "  /ayuda comandos        lista seca de comandos\n"
)

HELP_TOPICS = {
    "cargar": (
        "💸 Registrar movimientos\n"
        "\n"
        "Escribí lo que pasó y yo armo el movimiento:\n"
        "  MGB 5000 efectivo\n"
        "  egreso 12000 a Coca por transferencia\n"
        "  ingreso servicio del día 50000 nave\n"
        "\n"
        "Qué reconozco:\n"
        "  • Monto y fecha. Si no decís fecha, va la de hoy.\n"
        "  • Método de pago. Si no lo decís, asumo Efectivo.\n"
        "  • Proveedor o cliente.\n"
        "  • Unidad de negocio.\n"
        "\n"
        "Estado del pago:\n"
        "  • Por defecto queda PAGADO.\n"
        "  • Decí \"pendiente\", \"por pagar\" o \"a pagar\" para dejarlo pendiente.\n"
        "\n"
        "Notas: lo que escribas después de \"porque\", \"para\", \"nota:\" o entre\n"
        "paréntesis va como observación.\n"
        "  Ej: egreso 5000 a Coca por el evento de junio\n"
        "\n"
        "Fechas relativas como \"ayer\" o \"el martes\" también funcionan.\n"
    ),
    "adjuntos": (
        "📎 Fotos, PDF y audios\n"
        "\n"
        "Mandá el comprobante y leo los datos solo: monto, fecha, proveedor,\n"
        "CUIT, CBU, tipo y número de comprobante, método de pago.\n"
        "\n"
        "Podés mandarlo con texto o sin texto. Sin texto infiero todo del\n"
        "documento, incluso si es egreso o ingreso.\n"
        "\n"
        "⚠️ Mandá los comprobantes como ARCHIVO, no como Foto.\n"
        "Telegram comprime las fotos y un ticket con letra chica se vuelve\n"
        "ilegible. Como archivo llega intacto.\n"
        "\n"
        "Varios comprobantes: mandalos de a uno o en álbum. Cada archivo genera\n"
        "su propio movimiento.\n"
        "\n"
        "Cheques y eCheq: detecto la fecha de vencimiento aparte de la de\n"
        "emisión. Si el cheque es a fecha futura queda como pendiente.\n"
        "\n"
        "Audios: los transcribo y los trato como si los hubieras escrito.\n"
        "Sirve tanto para cargar un movimiento como para responder un menú.\n"
    ),
    "proveedores": (
        "👤 Cuando no reconozco al proveedor\n"
        "\n"
        "Si el nombre no matchea con tu catálogo, te muestro un menú numerado.\n"
        "Respondé con el número, nada más.\n"
        "\n"
        "Las opciones son:\n"
        "  1..5  los proveedores parecidos que encontré\n"
        "  •     Proveedor Varios — para gastos sueltos sin proveedor fijo\n"
        "  •     Crear nuevo — lo doy de alta con el nombre que leí\n"
        "  •     Dejar en blanco — se carga igual y lo completás en la app\n"
        "  •     Cancelar — no registra nada\n"
        "\n"
        "En un ingreso el menú dice cliente en vez de proveedor.\n"
        "\n"
        "Si no querés elegir ninguna, /cancelar.\n"
    ),
    "corregir": (
        "✏️ Corregir lo último\n"
        "\n"
        "Me acuerdo de los últimos mensajes de la conversación, así que podés\n"
        "referirte a lo anterior sin repetir todo:\n"
        "\n"
        "  \"ya lo pagué\"          → lo pasa a pagado\n"
        "  \"fue pagado\"           → igual\n"
        "  \"cambiale el proveedor a Coca\"\n"
        "  \"era para el otro proveedor\"\n"
        "  \"ese era de ayer\"\n"
        "\n"
        "Funciona con \"ese\", \"el último\", \"el de recién\".\n"
        "\n"
        "Ojo: hoy la corrección genera un movimiento corregido, no edita el\n"
        "original. Si ves algo duplicado, revisalo desde la app.\n"
    ),
    "transferencias": (
        "🔄 Transferencias internas\n"
        "\n"
        "Plata que se mueve entre TUS cajas o cuentas. No hay proveedor.\n"
        "\n"
        "  transferí 50000 de Caja Chica a Galicia\n"
        "  moví 20000 de Efectivo a Mercado Pago\n"
        "  pasé 100000 de Galicia a Caja Chica\n"
        "\n"
        "Si no reconozco alguna de las dos cajas te muestro un menú numerado,\n"
        "igual que con los proveedores.\n"
    ),
    "rrhh": (
        "👷 Adelantos y liquidaciones\n"
        "\n"
        "/adelanto <nombre> <monto> [nota]\n"
        "  Ej: /adelanto Juan Pérez 50000 para el alquiler\n"
        "  Registra el adelanto y lo descuenta en la próxima liquidación.\n"
        "\n"
        "/liquidar <nombre completo>\n"
        "  Ej: /liquidar Juan Pérez\n"
        "  Calcula la liquidación semanal y te muestra el detalle:\n"
        "  sueldo base, horas extra, adelantos aplicados y neto.\n"
        "  NO se confirma sola — respondé SI para confirmarla o NO para\n"
        "  descartarla.\n"
        "\n"
        "Sólo empleados en modalidad semanal.\n"
        "\n"
        "Si mandás otra cosa mientras hay una liquidación esperando, la\n"
        "liquidación NO se pierde: te la recuerdo hasta que la resuelvas.\n"
    ),
    "consultas": (
        "🔎 Consultas\n"
        "\n"
        "/resumen\n"
        "  Pagos pendientes: vencidos, próximos 7 días y próximos 30 días,\n"
        "  con total y proveedor de cada uno.\n"
        "\n"
        "Por ahora es la única consulta disponible. Preguntas del tipo\n"
        "\"cuánto gasté en insumos\" o \"cuánto le debo a X\" todavía no las\n"
        "puedo contestar — se consultan desde la app.\n"
    ),
    "comandos": (
        "⌨️ Comandos\n"
        "\n"
        "/ayuda [tema]   esta ayuda\n"
        "/resumen        pagos pendientes a 7 y 30 días\n"
        "/adelanto       /adelanto <nombre> <monto> [nota]\n"
        "/liquidar       /liquidar <nombre completo>\n"
        "/cancelar       cancela lo que haya pendiente\n"
        "\n"
        "Respuestas cortas que entiendo en contexto:\n"
        "  un número   elige una opción del menú\n"
        "  SI / NO     confirma o descarta una liquidación\n"
        "\n"
        "Todo lo demás es texto libre: escribí el movimiento como te salga.\n"
    ),
}

HELP_ALIASES = {
    "carga": "cargar",
    "movimientos": "cargar",
    "adjunto": "adjuntos",
    "fotos": "adjuntos",
    "audio": "adjuntos",
    "audios": "adjuntos",
    "comprobantes": "adjuntos",
    "proveedor": "proveedores",
    "clientes": "proveedores",
    "contrapartes": "proveedores",
    "menu": "proveedores",
    "menú": "proveedores",
    "correccion": "corregir",
    "corrección": "corregir",
    "corregirlo": "corregir",
    "editar": "corregir",
    "transferencia": "transferencias",
    "cajas": "transferencias",
    "sueldos": "rrhh",
    "liquidacion": "rrhh",
    "liquidación": "rrhh",
    "liquidaciones": "rrhh",
    "adelantos": "rrhh",
    "empleados": "rrhh",
    "consulta": "consultas",
    "reportes": "consultas",
    "comando": "comandos",
}


# Menú que Telegram autocompleta al escribir "/". Se publica al arrancar,
# así que agregar un comando acá alcanza para que aparezca en el chat.
BOT_COMMANDS = [
    ("/ayuda", "Qué sé hacer (/ayuda <tema> para el detalle)"),
    ("/resumen", "Pagos pendientes: vencidos, 7 y 30 días"),
    ("/recibos", "Recibos emitidos: /recibos <numero o proveedor>"),
    ("/adelanto", "Adelanto a empleado: /adelanto <nombre> <monto>"),
    ("/liquidar", "Liquidación semanal: /liquidar <nombre completo>"),
    ("/cancelar", "Cancela lo que haya pendiente"),
]


def build_help(argument: str = "") -> str:
    topic = (argument or "").strip().lower().lstrip("/")
    if not topic:
        return HELP_INDEX
    topic = HELP_ALIASES.get(topic, topic)
    if topic in HELP_TOPICS:
        return HELP_TOPICS[topic]
    return (
        f"No tengo un tema de ayuda para \"{argument.strip()}\".\n\n" + HELP_INDEX
    )


LIQ_CONFIRM_WORDS = {"si", "sí", "ok", "confirmar", "dale"}
LIQ_CANCEL_WORDS = {"no", "cancelar"}

# El estado de pendientes (movimiento, transferencia, liquidación) vive en el
# backend, no en memoria del proceso: sobrevive restarts y no depende de que la
# respuesta caiga en el mismo worker que originó la pregunta.


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
    return result.get("message") or "Liquidación preparada. Respondé SI para confirmar o NO para cancelar."


def build_receipt_document(phone: str, receipt_id: str | None = None,
                           movement_id: str | None = None, search: str | None = None) -> dict | None:
    """Pide el PDF al backend y arma la respuesta-documento. None si no se pudo.

    Nunca levanta: que falle el PDF no debe tumbar la respuesta del movimiento.
    """
    try:
        result = receipt_pdf(phone, receipt_id=receipt_id, movement_id=movement_id, search=search)
    except httpx.HTTPError:
        log.exception("no se pudo obtener el PDF del recibo")
        return None
    url = result.get("pdfUrl")
    if not url:
        return None
    receipt = result.get("receipt") or {}
    return {
        "document": url,
        "filename": result.get("fileName") or f"{receipt.get('number', 'recibo')}.pdf",
        "caption": f"Recibo {receipt.get('number', '')}".strip(),
    }


def handle_receipt_pdf_query(phone: str, search: str = "") -> list:
    """Manda el PDF del recibo que matchee. Si no matchea, lo dice en texto."""
    try:
        result = receipt_pdf(phone, search=search or None)
    except httpx.HTTPStatusError as e:
        detail, _ = _extract_edge_error(e)
        return [detail or "No pude encontrar ese recibo."]
    except httpx.HTTPError:
        log.exception("error pidiendo el PDF del recibo")
        return ["No pude generar el recibo. Probá de nuevo en un momento."]

    receipt = result.get("receipt") or {}
    document = {
        "document": result.get("pdfUrl"),
        "filename": result.get("fileName") or "recibo.pdf",
        "caption": f"Recibo {receipt.get('number', '')}".strip(),
    }
    if not document["document"]:
        return ["Encontré el recibo pero no pude generar el PDF."]
    return [document]


def handle_receipts_query(phone: str, argument: str = "") -> str:
    """Responde /recibos [texto]. Sin argumento devuelve los últimos emitidos."""
    search = (argument or "").strip()
    try:
        result = list_receipts(phone, search=search or None)
    except httpx.HTTPStatusError as e:
        detail, _ = _extract_edge_error(e)
        return detail or "No pude leer los recibos. Probá de nuevo en un momento."

    receipts = result.get("receipts") or []
    if not receipts:
        if search:
            return f"No encontré recibos que matcheen \"{search}\"."
        return "Todavía no hay recibos emitidos."

    header = f"Recibos que matchean \"{search}\":" if search else "Últimos recibos emitidos:"
    return header + "\n\n" + "\n\n".join(format_receipt_block(r) for r in receipts)


CANCEL_PENDING_REMINDER = (
    "⚠️ Seguís con una anulación esperando confirmación. "
    "Respondé SI para anular o NO para dejarlo como está."
)


def handle_cancel_request(phone: str) -> str:
    """Pide el último movimiento del bot y deja la anulación esperando el SI.

    No anula nada acá: el backend registra el pendiente y devuelve el resumen.
    """
    try:
        result = request_cancel_movement(phone)
    except httpx.HTTPStatusError as e:
        # La edge function responde {"error": "<texto para el usuario>"}, así que
        # el mensaje viaja en el primer elemento de la tupla, no en el segundo.
        detail, _ = _extract_edge_error(e)
        # 404 (no hay nada) y 409 (bloqueado por origen) ya vienen redactados
        # para el usuario final, así que se muestran tal cual.
        if e.response.status_code in (404, 409) and detail:
            return detail
        raise
    return (
        f"Vas a anular:\n{result.get('summary', '')}\n\n"
        "El movimiento queda anulado (no se borra) y sale de los saldos.\n"
        "Respondé SI para confirmar o NO para dejarlo como está."
    )


def handle_cancel_confirmation(phone: str, body: str, pending_cancellation: dict) -> str | None:
    """Resuelve SI/NO contra una anulación pendiente ya leída del backend.

    Devuelve el texto de respuesta si consumió el mensaje, o None si el mensaje no
    era una confirmación. En ese caso la anulación queda viva hasta que el usuario
    la resuelva o expire sola a los 30 minutos.
    """
    lower = (body or "").strip().lower()
    if lower in LIQ_CONFIRM_WORDS:
        confirm = True
    elif lower in LIQ_CANCEL_WORDS:
        confirm = False
    else:
        return None
    try:
        result = confirm_cancel_movement(phone, pending_cancellation["id"], confirm)
    except httpx.HTTPStatusError as e:
        detail, _ = _extract_edge_error(e)
        return detail or "No pude completar la anulación. Probá de nuevo."
    if not confirm:
        return "Listo, no anulé nada."
    return f"✅ Movimiento anulado:\n{result.get('summary', '')}"


def handle_liquidation_confirmation(phone: str, body: str, pending_liquidation: dict) -> str | None:
    """Resuelve SI/NO contra una liquidación pendiente ya leída del backend.

    Devuelve el texto de respuesta si consumió el mensaje, o None si el mensaje
    no era una confirmación. En ese caso la liquidación NO se descarta: queda
    viva hasta que el usuario la resuelva o expire sola en el backend.
    """
    lower = (body or "").strip().lower()
    if lower in LIQ_CONFIRM_WORDS:
        confirm = True
    elif lower in LIQ_CANCEL_WORDS:
        confirm = False
    else:
        return None
    try:
        result = rrhh_confirm_liquidation(phone, pending_liquidation["id"], confirm)
    except httpx.HTTPStatusError as e:
        code, msg = _extract_edge_error(e)
        if code == "duplicate_origin_ref":
            return ""
        return _map_rrhh_error(code, msg) or ""
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


HELP_COMMANDS = ("/ayuda", "/help", "/start")


def split_command(text: str) -> tuple[str, str]:
    """Separa "/ayuda cargar" en ("/ayuda", "cargar").

    Telegram agrega @nombre_del_bot al comando en grupos, así que se recorta.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return "", ""
    head, _, rest = stripped.partition(" ")
    return head.split("@")[0].lower(), rest.strip()


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
            # El backend todavia no genera PDF (payment_receipts.pdf_path esta
            # vacio en todas las filas): queda listo para cuando exista.
            "url": receipt.get("url") or receipt.get("pdfUrl"),
            "receiptKind": receipt.get("receiptKind"),
            "receiptDate": receipt.get("receiptDate"),
            "amount": receipt.get("amount"),
            "counterpartyName": receipt.get("counterpartyName"),
            "paymentMethod": receipt.get("paymentMethod"),
        }
    if result.get("receiptGenerated"):
        return {"id": None, "number": None, "url": None}
    return None


RECEIPT_KIND_LABELS = {"payment": "RECIBO DE PAGO", "income": "RECIBO DE COBRO"}


def format_receipt_block(receipt: dict) -> str:
    """Recibo formateado para el chat, a partir de una fila de payment_receipts.

    Texto plano a propósito: send_message no manda parse_mode, así que cualquier
    marca de formato se vería con los asteriscos literales.
    """
    number = receipt.get("number") or ""
    kind = receipt.get("receipt_kind") or receipt.get("receiptKind") or "payment"
    label = RECEIPT_KIND_LABELS.get(kind, "RECIBO")
    lines = [f"🧾 {label} {number}".strip()]

    date = receipt.get("receipt_date") or receipt.get("receiptDate")
    counterparty = receipt.get("counterparty_name") or receipt.get("counterpartyName")
    amount = receipt.get("amount")
    method = receipt.get("payment_method") or receipt.get("paymentMethod")

    if date:
        lines.append(f"Fecha:        {format_receipt_date(date)}")
    if counterparty:
        lines.append(f"Contraparte:  {counterparty}")
    if amount is not None:
        lines.append(f"Monto:        {format_amount(amount)}")
    if method:
        lines.append(f"Método:       {method}")
    return "\n".join(lines)


def format_receipt_date(value) -> str:
    """YYYY-MM-DD -> DD/MM/YYYY. Si no matchea, se devuelve tal cual."""
    text = str(value or "")
    parts = text.split("-")
    if len(parts) == 3 and len(parts[0]) == 4:
        return f"{parts[2][:2]}/{parts[1]}/{parts[0]}"
    return text


def format_receipt_line(result: dict | None) -> str:
    info = extract_receipt_info(result)
    if not info:
        return ""
    # Con los datos completos se muestra el recibo entero; si el backend sólo
    # mandó el flag, se cae al aviso de una línea de siempre.
    if info.get("number") and info.get("amount") is not None:
        return format_receipt_block(info)
    number = info.get("number")
    if number:
        return f"🧾 Comprobante #{number} generado"
    return "🧾 Comprobante generado"


def format_imputation_line(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    match = result.get("comprobanteMatch") or result.get("comprobante_match")
    if not isinstance(match, dict) or match.get("status") != "imputed":
        return ""
    imputation = match.get("imputation") or {}
    count = int(imputation.get("count") or 0)
    total = imputation.get("total") or 0
    if count <= 0:
        return ""
    label = "comprobante" if count == 1 else "comprobantes"
    return f"🧾 Imputado a {count} {label} ({format_amount(total)})"


def _format_comprobante_option(option: dict, idx: int) -> str:
    items = option.get("items") or []
    total = option.get("total") or sum(float(i.get("amountPending") or 0) for i in items)
    labels = []
    for item in items:
        doc = " ".join(str(x or "").strip() for x in [item.get("receiptType"), item.get("receiptNumber")]).strip()
        labels.append(doc or item.get("counterpartyName") or "Comprobante")
    return f"{idx}. {format_amount(total)} → {', '.join(labels[:3])}"


def format_comprobante_match_menu(match: dict | None) -> str:
    if not isinstance(match, dict) or match.get("status") != "needs_comprobante_selection":
        return ""
    options = match.get("options") or []
    visible = options[:5]
    lines = ["Encontré varias formas de imputar este pago."]
    lines.append("")
    lines.append("¿Cuál corresponde? Respondé con el número:")
    for idx, option in enumerate(visible, start=1):
        lines.append(_format_comprobante_option(option, idx))
    lines.append(f"{len(visible) + 1}. No imputar ahora")
    return "\n".join(lines)


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
    due_date = _get("due_date", "dueDate")
    classification = _get("classification_name", "classificationName")
    concept = _get("concept_name", "conceptName")
    payment_method = _get("payment_method_name", "paymentMethodName")
    business_unit = _get("business_unit_name", "businessUnitName")
    receipt_number = _get("receipt_number", "receiptNumber")

    cheque_number, notes_clean = parse_movement_notes_with_cheque(notes)

    emoji = "💸" if kind == "egreso" else "💰"
    lines = [f"{emoji} *{kind.upper()}* {format_amount(amount)}"]
    if cp:
        lines.append(f"👤 {cp}")
    if payment_method:
        lines.append(f"💳 {payment_method}")
    if cheque_number:
        lines.append(f"🔢 Cheque N° {cheque_number}")
    if date:
        lines.append(f"📅 {date}")
    if due_date and due_date != date:
        lines.append(f"⏰ Vence: {due_date}")
    if notes_clean:
        lines.append(f"📝 {notes_clean}")
    receipt_line = format_receipt_line(movement)
    if receipt_line:
        lines.append(receipt_line)
    imputation_line = format_imputation_line(movement)
    if imputation_line:
        lines.append(imputation_line)
    if movement.get("needs_counterparty"):
        lines.append(f"⚠ Completá el {_counterparty_label(kind)} desde la app")
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


def _counterparty_label(kind: str) -> str:
    return "cliente" if kind == "ingreso" else "proveedor"


def _build_menu_indexes(candidates: list, raw_name: str) -> dict:
    """Compute numeric menu indexes for pending selection.

    Shape: {varios, create (or None), skip, cancel, visible_count}.
    Mantiene sincronizados el armado del menú y su resolución.
    """
    visible_count = min(len(candidates or []), 5)
    next_idx = visible_count + 1
    varios_idx = next_idx
    next_idx += 1
    create_idx = next_idx if raw_name else None
    if raw_name:
        next_idx += 1
    skip_idx = next_idx
    next_idx += 1
    cancel_idx = next_idx
    return {
        "visible_count": visible_count,
        "varios": varios_idx,
        "create": create_idx,
        "skip": skip_idx,
        "cancel": cancel_idx,
    }


def format_transfer_candidate_menu(candidates: list, raw_name: str, unresolved_slot: str) -> str:
    slot_label = "origen" if unresolved_slot == "from" else "destino"
    lines = [f'No identifiqué la caja {slot_label} "{raw_name}".']
    lines.append("")
    lines.append("¿Cuál es? Respondé con el número:")
    for i, c in enumerate((candidates or [])[:5], start=1):
        lines.append(f"{i}. {c.get('name')}")
    cancel_idx = min(len(candidates or []), 5) + 1
    lines.append(f"{cancel_idx}. Cancelar")
    return "\n".join(lines)


def format_transfer_reply(result: dict) -> str:
    from_name = result.get("from_cash_box_name") or result.get("fromCashBoxName") or ""
    to_name = result.get("to_cash_box_name") or result.get("toCashBoxName") or ""
    amount = result.get("amount", 0)
    date = result.get("transfer_date") or result.get("transferDate") or ""
    notes = result.get("notes") or ""
    header = "⚠️ Transferencia ya registrada." if result.get("duplicated") else "🔄 *TRANSFERENCIA*"
    lines = [f"{header} {format_amount(amount)}"]
    if from_name or to_name:
        lines.append(f"📤 {from_name} → 📥 {to_name}")
    if date:
        lines.append(f"📅 {date}")
    if notes:
        lines.append(f"📝 {notes}")
    return "\n".join(lines)


def format_candidate_menu(candidates: list, suggested_name: str, suggested_cbu: str, kind: str = "egreso") -> str:
    label = _counterparty_label(kind)
    article = "la" if label == "cliente" else "al"
    lines = []
    if suggested_cbu:
        lines.append(f'No reconocí el {label} de "{suggested_name or suggested_cbu}".')
    elif suggested_name:
        lines.append(f'No reconocí {article} {label} "{suggested_name}".')
    else:
        lines.append(f"No encontré {label}.")
    lines.append("")
    lines.append("¿Cuál es? Respondé con el número:")
    for i, c in enumerate((candidates or [])[:5], start=1):
        lines.append(f"{i}. {c.get('name')}")
    idx = _build_menu_indexes(candidates, suggested_name)
    varios_label = "Cliente Varios" if label == "cliente" else "Proveedor Varios"
    lines.append(f"{idx['varios']}. {varios_label}")
    if idx["create"]:
        lines.append(f"{idx['create']}. Crear nuevo: {suggested_name}")
    lines.append(f"{idx['skip']}. Dejar en blanco (podés sumar nota: {idx['skip']} nota: ...)")
    lines.append(f"{idx['cancel']}. Cancelar")
    return "\n".join(lines)


NUMERIC_REPLY = re.compile(r"^\s*(\d+)\s*$")
SELECTION_REPLY = re.compile(r"^\s*(\d+)(?:[\s\.\)\]:;\-]+(?P<note>.*))?\s*$", re.DOTALL)
NOTE_PREFIX_RE = re.compile(r"^(?:nota|notas|obs|observacion|observación|comentario)\s*[:=\-]?\s*", re.IGNORECASE)


def try_parse_numeric(body: str) -> int | None:
    m = NUMERIC_REPLY.match(body or "")
    return int(m.group(1)) if m else None


def parse_pending_selection_reply(body: str) -> tuple[int, str] | None:
    m = SELECTION_REPLY.match(body or "")
    if not m:
        return None
    note = (m.group("note") or "").strip()
    note = NOTE_PREFIX_RE.sub("", note).strip()
    return int(m.group(1)), note


def with_optional_note(choice: dict, note: str) -> dict:
    if not note:
        return choice
    return {**choice, "notes": note}


def _resolve_transfer_pending(phone: str, choice_num: int, pending_transfer: dict) -> str:
    """Resolve a numeric reply against a pending transfer. Returns reply text."""
    candidates = pending_transfer.get("candidates") or []
    visible = candidates[:5]
    cancel_idx = len(visible) + 1

    if 1 <= choice_num <= len(visible):
        chosen = visible[choice_num - 1]
        result = confirm_transfer_pending(
            phone,
            pending_transfer["id"],
            {"kind": "select", "cashBoxId": chosen["id"]},
        )
    elif choice_num == cancel_idx:
        result = confirm_transfer_pending(phone, pending_transfer["id"], {"kind": "cancel"})
        if result.get("cancelled"):
            return "Transferencia cancelada."
        return "No pude cancelar."
    else:
        return "Opción inválida. Respondé con un número del menú o /cancelar."

    return format_transfer_reply(result)


def _cancel_pending_state(phone: str, pending_resp: dict) -> str:
    """Cancela todo lo que esté pendiente para este teléfono.

    Antes sólo intentaba el pendiente de movimiento: si lo único abierto era una
    transferencia o una liquidación, el /cancelar caía al parser de Claude y se
    interpretaba como un movimiento nuevo.
    """
    pending = pending_resp.get("pending")
    pending_transfer = pending_resp.get("pendingTransfer") or pending_resp.get("pending_transfer")
    pending_liquidation = pending_resp.get("pendingLiquidation")
    pending_cancellation = pending_resp.get("pendingCancellation")
    cancelled = []

    if pending:
        try:
            if confirm_pending(phone, {"kind": "cancel"}, pending_id=pending.get("id")).get("cancelled"):
                cancelled.append("la selección de contraparte")
        except httpx.HTTPStatusError:
            log.exception("no se pudo cancelar el pendiente de movimiento")
    if pending_transfer:
        try:
            if confirm_transfer_pending(phone, pending_transfer["id"], {"kind": "cancel"}).get("cancelled"):
                cancelled.append("la transferencia")
        except httpx.HTTPStatusError:
            log.exception("no se pudo cancelar el pendiente de transferencia")
    if pending_liquidation:
        try:
            if rrhh_confirm_liquidation(phone, pending_liquidation["id"], False).get("cancelled"):
                cancelled.append("la liquidación")
        except httpx.HTTPStatusError:
            log.exception("no se pudo cancelar la liquidación pendiente")
    if pending_cancellation:
        try:
            if confirm_cancel_movement(phone, pending_cancellation["id"], False).get("cancelled"):
                cancelled.append("la anulación")
        except httpx.HTTPStatusError:
            log.exception("no se pudo descartar la anulación pendiente")

    if not cancelled:
        return "No hay nada pendiente para cancelar."
    return "Cancelé " + " y ".join(cancelled) + "."


def _resolve_pending_choice(phone: str, body: str, state: dict | None = None) -> str | None:
    """Core pending-selection resolver. Returns plain reply text, or None if
    the message does not resolve a pending selection.

    `state` permite reusar un list_pending ya leído y evitar un round trip extra.
    """
    lower = (body or "").strip().lower()
    is_cancel = lower == "/cancelar"

    # parse_pending_selection_reply admite "3 con nota" -> (3, "con nota"), asi que
    # cubre lo que hacia try_parse_numeric y ademas rescata la nota del usuario.
    choice_num, choice_note = None, ""
    if not is_cancel:
        selection = parse_pending_selection_reply(body)
        if selection is None:
            return None
        choice_num, choice_note = selection

    pending_resp = state if state is not None else list_pending(phone)

    if is_cancel:
        # Cancela seleccion, transferencia y liquidacion de una, no solo la seleccion.
        return _cancel_pending_state(phone, pending_resp)

    pending = pending_resp.get("pending")
    pending_transfer = pending_resp.get("pendingTransfer") or pending_resp.get("pending_transfer")
    pending_comprobante = pending_resp.get("pendingComprobante") or pending_resp.get("pending_comprobante")

    if pending:
        candidates = pending.get("candidates") or []
        raw_name = pending.get("raw_counterparty_name") or ""
        idx = _build_menu_indexes(candidates, raw_name)
        visible = candidates[: idx["visible_count"]]

        if 1 <= choice_num <= idx["visible_count"]:
            cp = visible[choice_num - 1]
            result = confirm_pending(
                phone,
                with_optional_note({"kind": "existing", "counterpartyId": cp["id"]}, choice_note),
                pending_id=pending["id"],
            )
        elif choice_num == idx["varios"]:
            result = confirm_pending(phone, with_optional_note({"kind": "varios"}, choice_note), pending_id=pending["id"])
        elif idx["create"] and choice_num == idx["create"]:
            result = confirm_pending(phone, with_optional_note({"kind": "new", "name": raw_name}, choice_note), pending_id=pending["id"])
        elif choice_num == idx["skip"]:
            result = confirm_pending(phone, with_optional_note({"kind": "skip"}, choice_note), pending_id=pending["id"])
        elif choice_num == idx["cancel"]:
            result = confirm_pending(phone, {"kind": "cancel"}, pending_id=pending["id"])
            if result.get("cancelled"):
                return "Selección cancelada."
            return "No pude cancelar."
        else:
            return "Opción inválida. Respondé con un número del menú o /cancelar."

        log_receipt(result)
        if result.get("duplicated"):
            return format_duplicate_reply(result)
        comprobante_menu = format_comprobante_match_menu(result.get("comprobanteMatch"))
        if comprobante_menu:
            return f"{format_movement_reply(result)}\n\n{comprobante_menu}"
        return format_movement_reply(result)

    if pending_comprobante:
        options = pending_comprobante.get("options") or []
        skip_idx = min(len(options), 5) + 1
        if 1 <= choice_num <= min(len(options), 5):
            result = confirm_comprobante_pending(
                phone,
                pending_comprobante["id"],
                {"kind": "select", "optionIndex": choice_num},
            )
            return format_movement_reply(result)
        if choice_num == skip_idx:
            confirm_comprobante_pending(
                phone,
                pending_comprobante["id"],
                {"kind": "skip"},
            )
            return "Listo, no lo imputo ahora."
        return "Opción inválida. Respondé con un número del menú o /cancelar."

    if pending_transfer:
        return _resolve_transfer_pending(phone, choice_num, pending_transfer)

    return None


LIQ_PENDING_REMINDER = (
    "⚠️ Seguís con una liquidación esperando confirmación. "
    "Respondé SI para confirmarla o NO para cancelarla."
)


def fetch_pending_state(phone: str) -> dict:
    """Lee el estado de pendientes del backend. Nunca levanta: si falla, el
    mensaje sigue el flujo normal en vez de morir."""
    try:
        return list_pending(phone) or {}
    except httpx.HTTPStatusError:
        log.exception("no se pudo leer el estado de pendientes")
        return {}


def process_message(phone: str, body: str, sid: str, media_bytes: bytes | None, media_mime: str | None):
    """Registra UN movimiento (con o sin un adjunto). Devuelve el texto de respuesta."""
    log.info("process start | phone=%s body=%r mime=%s has_media=%s", phone, body[:80], media_mime, bool(media_bytes))

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

    if parsed.get("kind") == "recibo":
        log.info("routing to receipt_pdf | search=%r", parsed.get("search"))
        return handle_receipt_pdf_query(phone, (parsed.get("search") or "").strip())

    if parsed.get("kind") == "anular":
        log.info("routing to request_cancel_movement")
        reply = handle_cancel_request(phone)
        add_to_history(phone, "bot", reply)
        return reply

    if parsed.get("kind") == "transferencia":
        log.info("routing to internal_transfer")
        result = internal_transfer(
            phone=phone,
            origin_ref=sid,
            from_cash_box=parsed.get("fromCashBoxName") or "",
            to_cash_box=parsed.get("toCashBoxName") or "",
            amount=float(parsed.get("amount") or 0),
            transfer_date=parsed.get("movementDate") or datetime.now().strftime("%Y-%m-%d"),
            notes=parsed.get("notes"),
        )
        log.info("internal_transfer result: %s", json.dumps(result, ensure_ascii=False)[:300])
        if result.get("status") == "needs_selection_transfer":
            menu = format_transfer_candidate_menu(
                result.get("candidates") or [],
                result.get("rawName") or "",
                result.get("unresolvedSlot") or "from",
            )
            add_to_history(phone, "bot", menu)
            return menu
        reply = format_transfer_reply(result)
        add_to_history(phone, "bot", reply)
        return reply

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
            kind=parsed.get("kind") or "egreso",
        )
        add_to_history(phone, "bot", menu)
        return menu

    if result.get("duplicated"):
        dup_reply = format_duplicate_reply(result)
        add_to_history(phone, "bot", dup_reply)
        return dup_reply

    comprobante_menu = format_comprobante_match_menu(result.get("comprobanteMatch"))
    if comprobante_menu:
        reply = f"{format_movement_reply(result, parsed)}\n\n{comprobante_menu}"
        add_to_history(phone, "bot", reply)
        # Con menú abierto no se manda el PDF: primero que resuelva la imputación.
        return reply

    reply = format_movement_reply(result, parsed)
    add_to_history(phone, "bot", reply)

    # Si el movimiento generó recibo, se manda también el PDF. Va después del
    # texto para que el resumen se lea aunque el archivo tarde o falle.
    if extract_receipt_info(result):
        document = build_receipt_document(phone, movement_id=result.get("id"))
        if document:
            return [reply, document]
    return reply


def _flatten_replies(items: list) -> list:
    """Aplana los retornos de process_message.

    Devuelve un str cuando sólo hay texto, o [texto, documento] cuando además hay
    un PDF que mandar. Acá se normaliza a una lista plana de respuestas.
    """
    out = []
    for item in items:
        if isinstance(item, list):
            out.extend(item)
        elif item:
            out.append(item)
    return out


def handle_incoming(phone: str, body: str, sid: str, media_items: list[dict]) -> list[str]:
    """Rutea un mensaje entrante y devuelve la lista de respuestas a enviar."""
    audio_items = [m for m in media_items if is_audio_mime(m["mime"])]
    doc_items = [m for m in media_items if not is_audio_mime(m["mime"])]

    # El audio se transcribe y se suma al texto: de ahí en más es un mensaje de texto.
    for item in audio_items:
        log.info("transcribing audio (%d bytes, %s)", len(item["bytes"]), item["mime"])
        transcript = transcribe_audio(item["bytes"], item["mime"])
        log.info("transcript: %r", transcript[:200] if transcript else "")
        if not transcript:
            return ["No pude transcribir el audio. Mandalo otra vez o escribilo."]
        body = f"{body} {transcript}".strip() if body else transcript

    reminder = None

    if body and not doc_items:
        # El ruteo de comandos vive acá: Telegram no tiene respuesta síncrona,
        # todo sale por la API desde el thread de procesamiento.
        command, argument = split_command(body)
        if command in HELP_COMMANDS:
            return [build_help(argument)]
        if command == "/adelanto":
            return [handle_adelanto(phone, sid, body)]
        if command == "/liquidar":
            return [handle_liquidar(phone, sid, body)]
        if command == "/resumen":
            return [handle_summary(phone)]
        if command == "/recibos":
            return [handle_receipts_query(phone, argument)]
        if command and command != "/cancelar":
            return [f"No conozco el comando {command}.\n\n{HELP_INDEX}"]

        state = fetch_pending_state(phone)

        # La anulación se resuelve antes que nada: si hay una esperando, un "si"
        # tiene que anular y no caer al parser como si fuera un movimiento nuevo.
        pending_cancellation = state.get("pendingCancellation")
        if pending_cancellation:
            cancel_reply = handle_cancel_confirmation(phone, body, pending_cancellation)
            if cancel_reply is not None:
                return [cancel_reply]
            reminder = CANCEL_PENDING_REMINDER

        pending_liquidation = state.get("pendingLiquidation")
        if pending_liquidation:
            liq_reply = handle_liquidation_confirmation(phone, body, pending_liquidation)
            if liq_reply is not None:
                return [liq_reply]
            # No era SI/NO: la liquidación queda viva y se avisa al final.
            reminder = LIQ_PENDING_REMINDER

        resolved = _resolve_pending_choice(phone, body, state=state)
        if resolved is not None:
            return [resolved, reminder] if reminder else [resolved]

    if not body and not doc_items:
        return ["Mensaje vacío. Escribí /ayuda."]

    if not doc_items:
        replies = _flatten_replies([process_message(phone, body, sid, None, None)])
    else:
        # Cada adjunto es un comprobante distinto → un movimiento por adjunto.
        # El texto acompaña sólo al primero para no duplicar monto/nota en todos.
        # El originRef se sufija para que el dedupe del backend no los colapse.
        replies = [
            process_message(
                phone,
                body if i == 0 else "",
                sid if i == 0 else f"{sid}-{i}",
                item["bytes"],
                item["mime"],
            )
            for i, item in enumerate(doc_items)
        ]
        replies = _flatten_replies(replies)

    if reminder:
        replies.append(reminder)
    return replies


def user_facing_http_error(e: httpx.HTTPStatusError) -> str:
    """Traduce un error del backend a algo que le sirva al usuario.

    El detalle crudo (que expone internals de Supabase) va al log, no al chat.
    """
    code = e.response.status_code
    try:
        detail = e.response.json().get("error") or e.response.text
    except Exception:
        detail = e.response.text
    log.error("error del backend %s: %s", code, str(detail)[:500])
    if code == 403:
        return "Remitente no autorizado."
    if EXPOSE_ERROR_DETAIL:
        return f"Error ({code}): {str(detail)[:300]}"
    return "No pude registrar el movimiento. Quedó el error en el log; probá de nuevo en un momento."


def process_async(channel, phone: str, body: str, sid: str, media_refs: list[dict]):
    """Procesa en background y responde por el canal que trajo el mensaje.

    `channel` es telegram_api o twilio_api: sólo se le piden `download_media` y
    `send_message`, que ambos exponen igual. De ahí para abajo la lógica no sabe
    por dónde entró el mensaje.

    Va en un thread por los dos canales: Twilio corta a los 15s, y Telegram
    reintenta el update si el webhook no contesta 200 rápido —lo que duplicaría
    el movimiento—. Se contesta al toque y el trabajo real ocurre acá.
    """
    replies: list[str] = []
    try:
        try:
            media_items = channel.download_media(media_refs)
        except httpx.HTTPError:
            log.exception("no se pudieron descargar los adjuntos")
            replies = ["No pude descargar el adjunto. Mandalo de nuevo."]
        else:
            replies = handle_incoming(phone, body, sid, media_items)
    except httpx.HTTPStatusError as e:
        replies = [user_facing_http_error(e)]
    except json.JSONDecodeError:
        log.exception("respuesta del parser ilegible")
        replies = ["No pude interpretar el mensaje. Escribilo de otra forma."]
    except Exception:
        log.exception("async processing error")
        replies = ["Hubo un error procesando el mensaje. Probá de nuevo en un momento."]

    sent = 0
    for reply in replies:
        if not reply:
            continue
        # Una respuesta puede ser texto o un documento: {"document", "filename", "caption"}.
        if isinstance(reply, dict) and reply.get("document"):
            channel.send_document(
                phone, reply["document"], reply.get("filename") or "recibo.pdf", reply.get("caption") or "",
            )
        else:
            channel.send_message(phone, reply)
        sent += 1
    log.info("async reply sent to %s (%d mensajes)", phone, sent)


def _dispatch(channel, sender: str, body: str, ref: str, media_refs: list[dict]):
    threading.Thread(
        target=process_async,
        args=(channel, sender, body, ref, media_refs),
        daemon=True,
    ).start()


@app.route("/webhook/telegram", methods=["POST"])
def webhook_telegram():
    if not telegram_api.valid_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")):
        log.warning("secret token inválido | ip=%s", request.remote_addr)
        return "", 403

    message = telegram_api.extract_message(request.get_json(silent=True) or {})
    if not message:
        # Joins, callbacks, ediciones de otros tipos: nada que procesar.
        return "", 200

    chat_id = telegram_api.chat_id_of(message)
    if not chat_id:
        return "", 200

    body = telegram_api.extract_text(message)
    media_refs = telegram_api.extract_media_refs(message)
    log.info("telegram | chat_id=%s body=%r media=%d", chat_id, body[:80], len(media_refs))

    _dispatch(telegram_api, chat_id, body, telegram_api.message_ref(message), media_refs)

    # Siempre 200: un no-200 hace que Telegram reintente el update y duplique el
    # movimiento. Los errores se le informan al usuario desde process_async.
    return "", 200


# Se mantiene en /webhook (y no en /webhook/twilio) porque es la URL que ya está
# cargada en la consola de Twilio: moverla obligaría a reconfigurarla.
@app.route("/webhook", methods=["POST"])
def webhook_twilio():
    if not twilio_api.enabled():
        log.warning("llegó un webhook de Twilio con el canal apagado")
        return "", 503
    if not twilio_api.valid_signature():
        log.warning(
            "firma de Twilio inválida | ip=%s url=%s",
            request.remote_addr,
            twilio_api.public_request_url(),
        )
        return "", 403

    phone = twilio_api.sender_of()
    body = twilio_api.extract_text()
    total_media = twilio_api.num_media()
    media_refs, truncated = twilio_api.extract_media_refs(total_media)
    log.info("twilio | phone=%s body=%r media=%d", phone, body[:80], len(media_refs))

    if truncated:
        log.warning(
            "mensaje con %d adjuntos, se procesan los primeros %d",
            total_media,
            twilio_api.MAX_MEDIA_PER_MESSAGE,
        )
        twilio_api.send_message(
            phone,
            f"Recibí {total_media} adjuntos y proceso los primeros "
            f"{twilio_api.MAX_MEDIA_PER_MESSAGE}. Mandá el resto en otro mensaje.",
        )

    _dispatch(twilio_api, phone, body, twilio_api.message_ref(), media_refs)
    return "", 200


@app.route("/", methods=["GET"])
def health():
    return "Bot Finanzas OK", 200


def publish_bot_commands():
    """Sincroniza el menú de comandos al arrancar, en background.

    En un thread para no bloquear el boot de gunicorn si Telegram no responde.
    Sin TELEGRAM_BOT_TOKEN no hace ninguna llamada de red.
    """
    threading.Thread(
        target=telegram_api.set_my_commands,
        args=(BOT_COMMANDS,),
        daemon=True,
    ).start()


publish_bot_commands()


if __name__ == "__main__":
    app.run(debug=False, port=5000)
