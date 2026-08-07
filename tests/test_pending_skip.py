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
from app import _resolve_pending_choice, format_movement_reply
from conftest import make_prompt, pending_response


CANDIDATES = [{"id": "a", "name": "Proveedor A"}, {"id": "b", "name": "Proveedor B"}]


class _FakeConfirm:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, phone, choice, pending_id=None):
        self.calls.append({"phone": phone, "choice": choice, "pending_id": pending_id})
        return self.result


def _install_pending(monkeypatch, raw_name="Proveedor X", candidates=None):
    pending_obj = {
        "id": "pending-1",
        "candidates": candidates if candidates is not None else CANDIDATES,
        "raw_counterparty_name": raw_name,
    }
    response = pending_response(make_prompt("counterparty", pending_obj))
    monkeypatch.setattr(app, "list_pending", lambda phone: response)


def test_skip_choice_sends_kind_skip(monkeypatch):
    _install_pending(monkeypatch)
    fake_result = {
        "kind": "egreso",
        "amount": 5000,
        "movement_date": "2026-04-22",
        "needs_counterparty": True,
        "counterparty_name": None,
    }
    fake = _FakeConfirm(fake_result)
    monkeypatch.setattr(app, "confirm_pending", fake)

    # With 2 candidates + raw_name "Proveedor X":
    # 1,2 = candidatos; 3 = Varios; 4 = Crear; 5 = Skip; 6 = Cancelar
    reply = _resolve_pending_choice("+5490", "5")

    assert len(fake.calls) == 1
    assert fake.calls[0]["choice"] == {"kind": "skip"}
    assert fake.calls[0]["pending_id"] == "pending-1"
    assert "EGRESO" in reply
    assert "Completá el proveedor desde la app" in reply


def test_skip_choice_ingreso_uses_cliente(monkeypatch):
    _install_pending(monkeypatch)
    fake_result = {
        "kind": "ingreso",
        "amount": 10000,
        "movement_date": "2026-04-22",
        "needs_counterparty": True,
    }
    monkeypatch.setattr(app, "confirm_pending", _FakeConfirm(fake_result))

    reply = _resolve_pending_choice("+5490", "5")
    assert "INGRESO" in reply
    assert "Completá el cliente desde la app" in reply


def test_skip_choice_accepts_note_after_number(monkeypatch):
    _install_pending(monkeypatch)
    fake_result = {
        "kind": "egreso",
        "amount": 5000,
        "movement_date": "2026-04-22",
        "needs_counterparty": True,
        "counterparty_name": None,
        "notes": "sin proveedor claro",
    }
    fake = _FakeConfirm(fake_result)
    monkeypatch.setattr(app, "confirm_pending", fake)

    reply = _resolve_pending_choice("+5490", "5 nota: sin proveedor claro")

    assert len(fake.calls) == 1
    assert fake.calls[0]["choice"] == {"kind": "skip", "notes": "sin proveedor claro"}
    assert fake.calls[0]["pending_id"] == "pending-1"
    assert "sin proveedor claro" in reply


def test_cancel_still_works(monkeypatch):
    _install_pending(monkeypatch)
    fake = _FakeConfirm({"cancelled": True})
    monkeypatch.setattr(app, "confirm_pending", fake)

    # index 6 with 2 candidates + raw_name
    reply = _resolve_pending_choice("+5490", "6")
    assert fake.calls[0]["choice"] == {"kind": "cancel"}
    assert "cancelada" in reply.lower()


def test_invalid_number(monkeypatch):
    _install_pending(monkeypatch)
    monkeypatch.setattr(app, "confirm_pending", _FakeConfirm({}))
    reply = _resolve_pending_choice("+5490", "99")
    assert "inválida" in reply.lower()


def test_existing_candidate_by_number(monkeypatch):
    _install_pending(monkeypatch)
    fake_result = {
        "kind": "egreso",
        "amount": 3000,
        "counterparty_name": "Proveedor A",
    }
    fake = _FakeConfirm(fake_result)
    monkeypatch.setattr(app, "confirm_pending", fake)

    reply = _resolve_pending_choice("+5490", "1")
    assert fake.calls[0]["choice"] == {"kind": "existing", "counterpartyId": "a"}
    assert "Proveedor A" in reply


def test_existing_choice_accepts_note_after_number(monkeypatch):
    _install_pending(monkeypatch)
    fake_result = {
        "kind": "egreso",
        "amount": 3000,
        "counterparty_name": "Proveedor A",
        "notes": "corresponde a mayo",
    }
    fake = _FakeConfirm(fake_result)
    monkeypatch.setattr(app, "confirm_pending", fake)

    reply = _resolve_pending_choice("+5490", "1 obs: corresponde a mayo")

    assert fake.calls[0]["choice"] == {
        "kind": "existing",
        "counterpartyId": "a",
        "notes": "corresponde a mayo",
    }
    assert "corresponde a mayo" in reply


def test_skip_without_raw_name_is_index_4(monkeypatch):
    # With 2 candidates, no raw_name: 1,2 cands; 3 Varios; 4 Skip; 5 Cancel (no Crear nuevo)
    _install_pending(monkeypatch, raw_name="")
    fake_result = {"kind": "egreso", "amount": 100, "needs_counterparty": True}
    fake = _FakeConfirm(fake_result)
    monkeypatch.setattr(app, "confirm_pending", fake)

    reply = _resolve_pending_choice("+5490", "4")
    assert fake.calls[0]["choice"] == {"kind": "skip"}
    assert "Completá el proveedor desde la app" in reply


def test_movement_reply_without_needs_counterparty_no_warning():
    movement = {
        "kind": "egreso",
        "amount": 1500,
        "counterparty_name": "Proveedor X",
        "movement_date": "2026-04-22",
    }
    reply = format_movement_reply(movement)
    assert "Completá" not in reply


def test_movement_reply_with_needs_counterparty_warning_ingreso():
    movement = {
        "kind": "ingreso",
        "amount": 1500,
        "movement_date": "2026-04-22",
        "needs_counterparty": True,
    }
    reply = format_movement_reply(movement)
    assert "Completá el cliente desde la app" in reply
