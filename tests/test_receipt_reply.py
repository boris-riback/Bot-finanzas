import os
import sys
import pathlib

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+100000000")
os.environ.setdefault("BIALYSTOK_INGEST_URL", "https://test.invalid/ingest")
os.environ.setdefault("BOT_INGEST_TOKEN", "test")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import (
    extract_receipt_info,
    format_receipt_line,
    format_movement_reply,
    format_duplicate_reply,
)


def _base_movement():
    return {
        "kind": "egreso",
        "amount": 1500,
        "counterparty_name": "Proveedor X",
        "payment_method_name": "Efectivo",
        "movement_date": "2026-04-18",
    }


def test_extract_receipt_none_when_missing():
    assert extract_receipt_info({}) is None
    assert extract_receipt_info(None) is None
    assert extract_receipt_info({"foo": "bar"}) is None


def test_extract_receipt_flag_only():
    info = extract_receipt_info({"receiptGenerated": True})
    assert info == {"id": None, "number": None, "url": None}


def test_extract_receipt_object():
    info = extract_receipt_info({"receipt": {"id": "r-1", "number": "123", "url": "https://x/r-1.pdf"}})
    assert info == {"id": "r-1", "number": "123", "url": "https://x/r-1.pdf"}


def test_extract_receipt_pdf_url_alias():
    info = extract_receipt_info({"receipt": {"id": "r-1", "pdfUrl": "https://x/r-1.pdf"}})
    assert info["url"] == "https://x/r-1.pdf"


def test_format_receipt_line_with_number():
    assert format_receipt_line({"receipt": {"number": "123"}}) == "🧾 Comprobante #123 generado"


def test_format_receipt_line_flag_only():
    assert format_receipt_line({"receiptGenerated": True}) == "🧾 Comprobante generado"


def test_format_receipt_line_absent():
    assert format_receipt_line({}) == ""


def test_movement_reply_includes_receipt_with_number():
    mov = _base_movement() | {"receipt": {"id": "r-1", "number": "0001-00000042"}}
    reply = format_movement_reply(mov)
    assert "🧾 Comprobante #0001-00000042 generado" in reply


def test_movement_reply_includes_receipt_flag_only():
    mov = _base_movement() | {"receiptGenerated": True}
    reply = format_movement_reply(mov)
    assert "🧾 Comprobante generado" in reply


def test_movement_reply_without_receipt_unchanged():
    mov = _base_movement()
    reply = format_movement_reply(mov)
    assert "Comprobante" not in reply
    assert "💸" in reply
    assert "Proveedor X" in reply


# --- compat con shape de la app Bialystok (PaymentReceipt) ---

def test_receipt_number_app_format_rp_padded():
    """La app emite number='RP-000001'. Bot debe mostrarlo tal cual."""
    mov = _base_movement() | {
        "receiptGenerated": True,
        "receipt": {
            "id": "rcpt-uuid",
            "number": "RP-000042",
            "numberInt": 42,
            "receiptKind": "payment",
            "movementId": "mov-1",
            "created": True,
        },
    }
    reply = format_movement_reply(mov)
    assert "🧾 Comprobante #RP-000042 generado" in reply


def test_receipt_extra_fields_ignored_safely():
    """Campos adicionales (numberInt, receiptKind, movementId, created) no rompen."""
    info = extract_receipt_info({
        "receipt": {
            "id": "r1", "number": "RP-000001", "numberInt": 1,
            "receiptKind": "payment", "movementId": "m1", "created": True,
        },
    })
    assert info == {"id": "r1", "number": "RP-000001", "url": None}


def test_receipt_generated_false_no_line():
    """receiptGenerated=false (no aplicó la regla) — no muestra línea."""
    mov = _base_movement() | {"receiptGenerated": False}
    assert format_receipt_line(mov) == ""
    assert "Comprobante" not in format_movement_reply(mov)


# --- format_duplicate_reply ---

def test_duplicate_reply_header_only_when_no_movement():
    assert format_duplicate_reply(None) == "⚠️ Movimiento ya registrado previamente."
    assert format_duplicate_reply({}) == "⚠️ Movimiento ya registrado previamente."


def test_duplicate_reply_includes_movement_detail():
    mov = _base_movement() | {"duplicated": True}
    reply = format_duplicate_reply(mov)
    assert reply.startswith("⚠️ Movimiento ya registrado previamente.")
    assert "Proveedor X" in reply
    assert "💸" in reply


def test_duplicate_reply_includes_existing_receipt():
    """Si la app emite el receipt preexistente en dedupe, el bot lo muestra."""
    mov = _base_movement() | {
        "duplicated": True,
        "receipt": {"id": "r1", "number": "RP-000099"},
    }
    reply = format_duplicate_reply(mov)
    assert "⚠️ Movimiento ya registrado previamente." in reply
    assert "🧾 Comprobante #RP-000099 generado" in reply
