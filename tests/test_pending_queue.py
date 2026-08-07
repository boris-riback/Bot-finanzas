import os
import sys
import pathlib

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+100000000")
os.environ.setdefault("BIALYSTOK_INGEST_URL", "https://test.invalid/ingest")
os.environ.setdefault("BOT_INGEST_TOKEN", "test")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import app
from app import _resolve_pending_choice, format_pending_prompt, with_next_prompt
from conftest import make_prompt, pending_response


COUNTERPARTY_ROW = {
    "id": "mp-1",
    "candidates": [{"id": "cp-a", "name": "Vittorio's SRL"}],
    "raw_counterparty_name": "Vittorios",
    "payload": {"counterpartyName": "Vittorio's", "amount": 920509.32, "kind": "egreso"},
}

COMPROBANTE_ROW = {
    "id": "imp-1",
    "options": [
        {"total": 920509.32, "reason": "monto_exacto", "items": [{"receiptType": "Factura A", "receiptNumber": "8849"}]},
        {"total": 920509.32, "reason": "suma_exacta", "items": [{"receiptType": "Factura A", "receiptNumber": "8850"}]},
    ],
    "payload": {"counterpartyName": "Vittorio's", "amount": 920509.32, "kind": "egreso"},
}

SUBJECT = {"counterpartyName": "Vittorio's", "amount": 920509.32, "movementKind": "egreso"}


def test_prompt_says_which_comprobante_it_is_asking_about():
    prompt = make_prompt("comprobante", COMPROBANTE_ROW, subject=SUBJECT)
    prompt = {**prompt, "position": 2, "total": 3}

    text = format_pending_prompt(prompt)

    assert "Pregunta 2 de 3" in text
    assert "Vittorio's" in text
    assert "Encontré varias formas de imputar" in text
    assert "8849" in text


def test_single_open_question_has_no_position_header():
    text = format_pending_prompt(make_prompt("comprobante", COMPROBANTE_ROW, subject=SUBJECT))

    assert "Pregunta" not in text
    assert "Vittorio's" in text


def test_prompt_formats_each_kind():
    counterparty = format_pending_prompt(make_prompt("counterparty", COUNTERPARTY_ROW))
    transfer = format_pending_prompt(make_prompt("transfer", {
        "id": "tp-1",
        "candidates": [{"id": "cb-a", "name": "Banco Galicia"}],
        "unresolved_slot": "to",
        "raw_name": "galicia",
    }))

    assert "Vittorio's SRL" in counterparty
    assert "destino" in transfer.lower()
    assert format_pending_prompt(None) == ""
    assert format_pending_prompt({"kind": "otra_cosa", "data": {}}) == ""


def test_reply_chains_the_next_question():
    result = {"nextPending": make_prompt("comprobante", COMPROBANTE_ROW, subject=SUBJECT)}

    text = with_next_prompt("✅ Cargado", result)

    assert text.startswith("✅ Cargado")
    assert "Encontré varias formas de imputar" in text


def test_reply_without_queue_stays_untouched():
    assert with_next_prompt("✅ Cargado", {}) == "✅ Cargado"
    assert with_next_prompt("✅ Cargado", None) == "✅ Cargado"


def test_answer_resolves_the_queued_question_and_shows_the_following_one(monkeypatch):
    """Dos comprobantes seguidos: el número contesta el primero y aparece el segundo."""
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response(
        make_prompt("comprobante", COMPROBANTE_ROW, subject=SUBJECT),
        make_prompt("counterparty", COUNTERPARTY_ROW, subject=SUBJECT),
    ))

    calls = []

    def fake_confirm_comprobante(phone, pending_id, choice):
        calls.append({"pending_id": pending_id, "choice": choice})
        return {
            "kind": "egreso",
            "amount": 920509.32,
            "counterparty_name": "Vittorio's",
            # Al resolver la primera, el backend devuelve la que sigue.
            "nextPending": {**make_prompt("counterparty", COUNTERPARTY_ROW, subject=SUBJECT), "position": 1, "total": 1},
        }

    monkeypatch.setattr(app, "confirm_comprobante_pending", fake_confirm_comprobante)
    monkeypatch.setattr(app, "confirm_pending", lambda *a, **kw: {"fail": True})

    reply = _resolve_pending_choice("+5490", "1")

    assert calls == [{"pending_id": "imp-1", "choice": {"kind": "select", "optionIndex": 1}}]
    assert "EGRESO" in reply
    assert "Vittorio's SRL" in reply  # el menú de la siguiente pregunta va encadenado


def test_skipping_an_imputation_also_chains_the_next_one(monkeypatch):
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response(
        make_prompt("comprobante", COMPROBANTE_ROW, subject=SUBJECT),
    ))
    monkeypatch.setattr(app, "confirm_comprobante_pending", lambda phone, pending_id, choice: {
        "skipped": True,
        "nextPending": make_prompt("counterparty", COUNTERPARTY_ROW, subject=SUBJECT),
    })

    # 2 opciones → el 3 es "No imputar ahora"
    reply = _resolve_pending_choice("+5490", "3")

    assert "no lo imputo ahora" in reply.lower()
    assert "Vittorio's SRL" in reply
