import os
import sys
import pathlib

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+100000000")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from flask import Flask

import twilio_api

flask_app = Flask(__name__)


def _post(form: dict, headers: dict | None = None):
    """Contexto de request para las funciones que leen del form de Flask."""
    return flask_app.test_request_context(
        "/webhook", method="POST", data=form, headers=headers or {}
    )


def test_enabled_con_credenciales():
    assert twilio_api.enabled() is True


def test_sender_saca_el_prefijo_whatsapp():
    with _post({"From": "whatsapp:+5493532403555"}):
        assert twilio_api.sender_of() == "+5493532403555"


def test_sender_vacio_si_no_viene():
    with _post({}):
        assert twilio_api.sender_of() == ""


def test_extract_text_y_message_ref():
    with _post({"Body": "  nafta 5000  ", "MessageSid": "SM123"}):
        assert twilio_api.extract_text() == "nafta 5000"
        assert twilio_api.message_ref() == "SM123"


def test_num_media_tolera_basura():
    with _post({"NumMedia": "2"}):
        assert twilio_api.num_media() == 2
    with _post({"NumMedia": "no-es-un-numero"}):
        assert twilio_api.num_media() == 0
    with _post({}):
        assert twilio_api.num_media() == 0


def test_extract_media_refs_junta_todos():
    form = {
        "NumMedia": "2",
        "MediaUrl0": "https://api.twilio.test/a",
        "MediaContentType0": "image/jpeg",
        "MediaUrl1": "https://api.twilio.test/b",
        "MediaContentType1": "application/pdf",
    }
    with _post(form):
        refs, truncated = twilio_api.extract_media_refs(2)
    assert truncated is False
    assert refs == [
        {"url": "https://api.twilio.test/a", "mime": "image/jpeg"},
        {"url": "https://api.twilio.test/b", "mime": "application/pdf"},
    ]


def test_extract_media_refs_trunca_en_el_maximo():
    total = twilio_api.MAX_MEDIA_PER_MESSAGE + 3
    form = {"NumMedia": str(total)}
    for i in range(total):
        form[f"MediaUrl{i}"] = f"https://api.twilio.test/{i}"
        form[f"MediaContentType{i}"] = "image/jpeg"
    with _post(form):
        refs, truncated = twilio_api.extract_media_refs(total)
    assert truncated is True
    assert len(refs) == twilio_api.MAX_MEDIA_PER_MESSAGE


def test_extract_media_refs_sin_adjuntos():
    with _post({"NumMedia": "0"}):
        refs, truncated = twilio_api.extract_media_refs(0)
    assert refs == []
    assert truncated is False


def test_public_request_url_usa_los_forwarded_de_render():
    # Render termina TLS en su proxy: sin esto la URL llegaría como http:// y
    # toda firma válida sería rechazada.
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "bot.onrender.com"}
    with _post({}, headers):
        assert twilio_api.public_request_url() == "https://bot.onrender.com/webhook"


def test_valid_signature_rechaza_sin_header():
    with _post({"Body": "hola"}):
        assert twilio_api.valid_signature() is False


def test_valid_signature_rechaza_firma_falsa():
    with _post({"Body": "hola"}, {"X-Twilio-Signature": "firma-inventada"}):
        assert twilio_api.valid_signature() is False


def test_valid_signature_acepta_firma_real():
    # Se firma con el mismo algoritmo que usa Twilio, con el AUTH_TOKEN de test.
    from twilio.request_validator import RequestValidator

    url = "https://bot.onrender.com/webhook"
    form = {"Body": "hola", "From": "whatsapp:+549353"}
    signature = RequestValidator("test").compute_signature(url, form)
    headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "bot.onrender.com",
        "X-Twilio-Signature": signature,
    }
    with _post(form, headers):
        assert twilio_api.valid_signature() is True
