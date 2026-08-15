"""Endpoint /parse: el cerebro del bot expuesto para el importador de la app.

Lo que se protege acá es que /parse NO cargue nada: sólo devuelve el borrador.
El guardado lo hace la app con la sesión del usuario, así que un bug que lo
convierta en un ingest encubierto es exactamente lo que no se puede escapar.
"""
import base64
import json
import os
import pathlib
import sys

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+100000000")
os.environ.setdefault("BIALYSTOK_INGEST_URL", "https://test.invalid/ingest")
os.environ.setdefault("BOT_INGEST_TOKEN", "test")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

TOKEN = "app-token-secreto"
CATALOG = {"businessUnits": [{"id": "bu-1", "name": "Bar"}], "counterparties": []}
PDF_BYTES = b"%PDF-1.4 comprobante"
PARSED = {
    "kind": "egreso",
    "amount": 15000,
    "counterpartyName": "Distribuidora MGB",
    "movementDate": "2026-08-14",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, "APP_PARSE_TOKEN", TOKEN)
    return app.app.test_client()


def _body(**overrides) -> dict:
    payload = {
        "catalog": CATALOG,
        "body": "",
        "mime": "application/pdf",
        "base64": base64.b64encode(PDF_BYTES).decode(),
    }
    payload.update(overrides)
    return payload


def _post(client, payload, token=TOKEN):
    headers = {"X-App-Token": token} if token is not None else {}
    return client.post("/parse", json=payload, headers=headers)


def test_devuelve_el_borrador_y_le_pasa_los_bytes_del_pdf(client, monkeypatch):
    calls = []

    def fake_parse(body, catalog, media_bytes, media_mime, phone=""):
        calls.append({"body": body, "catalog": catalog, "bytes": media_bytes,
                      "mime": media_mime, "phone": phone})
        return PARSED

    monkeypatch.setattr(app, "claude_parse", fake_parse)

    response = _post(client, _body(body="pagado por transferencia"))

    assert response.status_code == 200
    assert response.get_json() == {"parsed": PARSED}
    assert len(calls) == 1
    assert calls[0]["bytes"] == PDF_BYTES
    assert calls[0]["mime"] == "application/pdf"
    assert calls[0]["body"] == "pagado por transferencia"
    assert calls[0]["catalog"] == CATALOG
    # Sin phone no hay historial de chat: la app no tiene conversación.
    assert calls[0]["phone"] == ""


def test_no_carga_nada_en_el_backend(client, monkeypatch):
    """El endpoint lee y contesta; cualquier llamada al ERP sería un bug."""
    monkeypatch.setattr(app, "claude_parse", lambda *a, **k: PARSED)
    for name in ("ingest", "upload_attachment", "internal_transfer", "fetch_catalog"):
        monkeypatch.setattr(app, name, _boom(name))

    assert _post(client, _body()).status_code == 200


def _boom(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"/parse no debe llamar a {name}")
    return _raise


def test_acepta_imagenes(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(app, "claude_parse", lambda body, cat, b, m, phone="": seen.update(mime=m) or PARSED)

    response = _post(client, _body(mime="image/jpeg"))

    assert response.status_code == 200
    assert seen["mime"] == "image/jpeg"


def test_normaliza_el_mime_con_charset(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(app, "claude_parse", lambda body, cat, b, m, phone="": seen.update(mime=m) or PARSED)

    response = _post(client, _body(mime="application/pdf; charset=binary"))

    assert response.status_code == 200
    assert seen["mime"] == "application/pdf"


def test_rechaza_tipo_no_soportado(client, monkeypatch):
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))

    response = _post(client, _body(mime="application/zip"))

    assert response.status_code == 400
    assert "no soportado" in response.get_json()["error"]


def test_rechaza_archivo_ilegible(client, monkeypatch):
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))

    response = _post(client, _body(base64="no-es-base64!!"))

    assert response.status_code == 400


def test_rechaza_archivo_gigante(client, monkeypatch):
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))
    gordo = base64.b64encode(b"x" * (app.MAX_PARSE_BYTES + 1)).decode()

    response = _post(client, _body(base64=gordo))

    assert response.status_code == 400
    assert "15 MB" in response.get_json()["error"]


def test_rechaza_pedido_vacio(client, monkeypatch):
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))

    response = _post(client, _body(base64="", body=""))

    assert response.status_code == 400


def test_rechaza_sin_catalogo(client, monkeypatch):
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))

    response = _post(client, _body(catalog=None))

    assert response.status_code == 400


def test_acepta_solo_texto_sin_adjunto(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(app, "claude_parse", lambda body, cat, b, m, phone="": seen.update(bytes=b, mime=m) or PARSED)

    response = _post(client, _body(base64="", mime="", body="MGB 5000 efectivo"))

    assert response.status_code == 200
    assert seen["bytes"] is None
    assert seen["mime"] is None


def test_token_invalido(client, monkeypatch):
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))

    assert _post(client, _body(), token="otro").status_code == 401
    assert _post(client, _body(), token=None).status_code == 401


def test_sin_token_configurado_no_atiende(monkeypatch):
    """Si falta la env var el endpoint se apaga, no queda abierto."""
    monkeypatch.setattr(app, "APP_PARSE_TOKEN", "")
    monkeypatch.setattr(app, "claude_parse", _boom("claude_parse"))

    response = app.app.test_client().post("/parse", json=_body(), headers={"X-App-Token": ""})

    assert response.status_code == 503


def test_claude_devuelve_basura(client, monkeypatch):
    def fake_parse(*args, **kwargs):
        raise json.JSONDecodeError("no json", "", 0)

    monkeypatch.setattr(app, "claude_parse", fake_parse)

    response = _post(client, _body())

    assert response.status_code == 502
    assert "error" in response.get_json()


def test_claude_se_cae(client, monkeypatch):
    def fake_parse(*args, **kwargs):
        raise RuntimeError("API caída")

    monkeypatch.setattr(app, "claude_parse", fake_parse)

    response = _post(client, _body())

    assert response.status_code == 502
