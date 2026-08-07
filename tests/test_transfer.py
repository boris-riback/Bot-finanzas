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
from app import (
    format_transfer_candidate_menu,
    format_transfer_reply,
    _resolve_pending_choice,
)
from conftest import make_prompt, pending_response


def test_transfer_reply_happy_path():
    result = {
        "from_cash_box_name": "Caja Chica",
        "to_cash_box_name": "Banco Galicia",
        "amount": 50000,
        "transfer_date": "2026-04-22",
        "duplicated": False,
    }
    reply = format_transfer_reply(result)
    assert "TRANSFERENCIA" in reply
    assert "Caja Chica" in reply
    assert "Banco Galicia" in reply
    assert "$50,000" in reply
    assert "2026-04-22" in reply


def test_transfer_reply_duplicated():
    result = {
        "fromCashBoxName": "A",
        "toCashBoxName": "B",
        "amount": 1000,
        "duplicated": True,
    }
    reply = format_transfer_reply(result)
    assert "ya registrada" in reply.lower()


def test_transfer_reply_with_notes():
    result = {
        "fromCashBoxName": "A",
        "toCashBoxName": "B",
        "amount": 1000,
        "notes": "para sueldos",
    }
    reply = format_transfer_reply(result)
    assert "para sueldos" in reply


def test_transfer_menu_from_slot():
    candidates = [{"id": "x", "name": "Caja Chica Local"}, {"id": "y", "name": "Caja Chica Banco"}]
    menu = format_transfer_candidate_menu(candidates, "caja chica", "from")
    assert "origen" in menu.lower()
    assert "Caja Chica Local" in menu
    assert "Caja Chica Banco" in menu
    assert "3. Cancelar" in menu


def test_transfer_menu_to_slot():
    candidates = [{"id": "x", "name": "Banco Galicia"}]
    menu = format_transfer_candidate_menu(candidates, "banco", "to")
    assert "destino" in menu.lower()
    assert "2. Cancelar" in menu


def test_resolve_transfer_pending_select(monkeypatch):
    pending_transfer = {
        "id": "pt-1",
        "candidates": [{"id": "cb-a", "name": "Banco A"}, {"id": "cb-b", "name": "Banco B"}],
        "unresolved_slot": "to",
    }
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response(make_prompt("transfer", pending_transfer)))

    calls = []

    def fake_confirm_transfer(phone, pending_id, choice):
        calls.append({"phone": phone, "pending_id": pending_id, "choice": choice})
        return {
            "fromCashBoxName": "Caja Chica",
            "toCashBoxName": "Banco B",
            "amount": 5000,
            "transferDate": "2026-04-22",
        }

    monkeypatch.setattr(app, "confirm_transfer_pending", fake_confirm_transfer)

    reply = _resolve_pending_choice("+5490", "2")
    assert len(calls) == 1
    assert calls[0]["choice"] == {"kind": "select", "cashBoxId": "cb-b"}
    assert calls[0]["pending_id"] == "pt-1"
    assert "Banco B" in reply
    assert "TRANSFERENCIA" in reply


def test_resolve_transfer_pending_cancel(monkeypatch):
    pending_transfer = {
        "id": "pt-1",
        "candidates": [{"id": "cb-a", "name": "A"}],
        "unresolved_slot": "from",
    }
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response(make_prompt("transfer", pending_transfer)))

    def fake_confirm_transfer(phone, pending_id, choice):
        return {"cancelled": True}

    monkeypatch.setattr(app, "confirm_transfer_pending", fake_confirm_transfer)

    # 1 candidate → cancel is index 2
    reply = _resolve_pending_choice("+5490", "2")
    assert "cancelada" in reply.lower()


def test_resolve_transfer_pending_invalid(monkeypatch):
    pending_transfer = {
        "id": "pt-1",
        "candidates": [{"id": "cb-a", "name": "A"}],
        "unresolved_slot": "from",
    }
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response(make_prompt("transfer", pending_transfer)))
    monkeypatch.setattr(app, "confirm_transfer_pending", lambda *a, **kw: {})

    reply = _resolve_pending_choice("+5490", "99")
    assert "inválida" in reply.lower()


def test_oldest_question_wins_over_table_order(monkeypatch):
    """La respuesta va a la pregunta más vieja, sin importar de qué cola sea.

    Antes ganaba siempre la de contraparte por estar chequeada primero: con una
    transferencia preguntada antes, el número contestaba la pregunta equivocada.
    """
    movement_pending = {
        "id": "mp-1",
        "candidates": [{"id": "cp-a", "name": "Proveedor A"}],
        "raw_counterparty_name": "Prov",
    }
    transfer_pending = {
        "id": "tp-1",
        "candidates": [{"id": "cb-a", "name": "Caja X"}],
        "unresolved_slot": "from",
    }
    # La transferencia entró primero, así que es la que se responde.
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response(
        make_prompt("transfer", transfer_pending),
        make_prompt("counterparty", movement_pending),
    ))

    transfer_calls = []

    def fake_confirm_transfer(phone, pending_id, choice):
        transfer_calls.append({"pending_id": pending_id, "choice": choice})
        return {"fromCashBoxName": "Caja X", "toCashBoxName": "Banco", "amount": 100}

    monkeypatch.setattr(app, "confirm_transfer_pending", fake_confirm_transfer)
    monkeypatch.setattr(app, "confirm_pending", lambda *a, **kw: {"fail": True})

    reply = _resolve_pending_choice("+5490", "1")

    assert len(transfer_calls) == 1
    assert transfer_calls[0]["pending_id"] == "tp-1"
    assert "TRANSFERENCIA" in reply


def test_no_pending_returns_none(monkeypatch):
    monkeypatch.setattr(app, "list_pending", lambda phone: pending_response())
    assert _resolve_pending_choice("+5490", "1") is None
