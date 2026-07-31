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

import httpx
import pytest

import app

PENDING = {"id": "pc-1", "movement_id": "mov-1", "summary": "Egreso $5.000 · MGB · Efectivo"}


class _Recorder:
    """Reemplaza al cliente del backend y guarda con qué lo llamaron."""

    def __init__(self, result=None, raises=None):
        self.result = result or {}
        self.raises = raises
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.raises:
            raise self.raises
        return self.result


def _http_error(status: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://test.invalid/ingest")
    response = httpx.Response(status, json={"error": message}, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


# ── request ──────────────────────────────────────────────────────────────────

def test_request_pide_confirmacion_y_no_anula(monkeypatch):
    rec = _Recorder({"status": "needs_confirmation", "pendingId": "pc-1", "summary": "Egreso $5.000 · MGB"})
    monkeypatch.setattr(app, "request_cancel_movement", rec)
    reply = app.handle_cancel_request("+549")
    assert "Egreso $5.000 · MGB" in reply
    assert "SI" in reply and "NO" in reply
    # Debe dejar claro que anula, no borra.
    assert "no se borra" in reply
    assert rec.calls == [("+549",)]


def test_request_sin_movimientos_muestra_el_mensaje_del_backend(monkeypatch):
    monkeypatch.setattr(
        app, "request_cancel_movement",
        _Recorder(raises=_http_error(404, "No encontré ningún movimiento cargado por el bot para anular.")),
    )
    assert "No encontré" in app.handle_cancel_request("+549")


def test_request_bloqueado_muestra_el_motivo(monkeypatch):
    monkeypatch.setattr(
        app, "request_cancel_movement",
        _Recorder(raises=_http_error(409, "Los movimientos de Inversiones se anulan desde ese módulo.")),
    )
    assert "Inversiones" in app.handle_cancel_request("+549")


def test_request_propaga_errores_inesperados(monkeypatch):
    monkeypatch.setattr(app, "request_cancel_movement", _Recorder(raises=_http_error(500, "boom")))
    with pytest.raises(httpx.HTTPStatusError):
        app.handle_cancel_request("+549")


# ── confirmación ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["si", "sí", "SI", " Dale ", "ok", "confirmar"])
def test_confirmacion_positiva_anula(monkeypatch, word):
    rec = _Recorder({"annulled": True, "summary": "Egreso $5.000 · MGB"})
    monkeypatch.setattr(app, "confirm_cancel_movement", rec)
    reply = app.handle_cancel_confirmation("+549", word, PENDING)
    assert "anulado" in reply.lower()
    assert rec.calls == [("+549", "pc-1", True)]


@pytest.mark.parametrize("word", ["no", "NO", "cancelar"])
def test_confirmacion_negativa_no_anula(monkeypatch, word):
    rec = _Recorder({"cancelled": True})
    monkeypatch.setattr(app, "confirm_cancel_movement", rec)
    reply = app.handle_cancel_confirmation("+549", word, PENDING)
    assert reply == "Listo, no anulé nada."
    assert rec.calls == [("+549", "pc-1", False)]


def test_mensaje_que_no_es_confirmacion_devuelve_none(monkeypatch):
    rec = _Recorder({})
    monkeypatch.setattr(app, "confirm_cancel_movement", rec)
    # None = "no consumí el mensaje": el pendiente queda vivo y sigue el flujo normal.
    assert app.handle_cancel_confirmation("+549", "nafta 3000 efectivo", PENDING) is None
    assert rec.calls == []


def test_confirmacion_con_error_del_backend_no_explota(monkeypatch):
    monkeypatch.setattr(
        app, "confirm_cancel_movement",
        _Recorder(raises=_http_error(409, "Ese movimiento ya está anulado.")),
    )
    assert "ya está anulado" in app.handle_cancel_confirmation("+549", "si", PENDING)


def test_usa_el_mismo_vocabulario_que_las_liquidaciones():
    # Un solo set de palabras: que SI signifique lo mismo en los dos flujos.
    assert "si" in app.LIQ_CONFIRM_WORDS
    assert "no" in app.LIQ_CANCEL_WORDS
