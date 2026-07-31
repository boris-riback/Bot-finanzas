import os
import sys
import pathlib

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+100000000")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import telegram_api


def test_valid_secret_ok():
    assert telegram_api.valid_secret("test-secret") is True


def test_valid_secret_rechaza_distinto():
    assert telegram_api.valid_secret("otro") is False


def test_valid_secret_rechaza_vacio():
    assert telegram_api.valid_secret("") is False


def test_extract_message_toma_message_y_edited():
    assert telegram_api.extract_message({"message": {"a": 1}}) == {"a": 1}
    assert telegram_api.extract_message({"edited_message": {"b": 2}}) == {"b": 2}
    assert telegram_api.extract_message({"callback_query": {}}) is None
    assert telegram_api.extract_message({}) is None


def test_chat_id_y_message_ref():
    msg = {"chat": {"id": 6930157786}, "message_id": 42}
    assert telegram_api.chat_id_of(msg) == "6930157786"
    # Estable y repetible: el backend lo usa como originRef para deduplicar.
    assert telegram_api.message_ref(msg) == "tg-6930157786-42"


def test_extract_text_usa_caption_cuando_hay_adjunto():
    assert telegram_api.extract_text({"text": "hola"}) == "hola"
    assert telegram_api.extract_text({"caption": "nafta 5000"}) == "nafta 5000"
    assert telegram_api.extract_text({}) == ""


def test_extract_text_saca_el_sufijo_del_bot_en_comandos():
    assert telegram_api.extract_text({"text": "/resumen@FinanzasBialyBot"}) == "/resumen"
    assert (
        telegram_api.extract_text({"text": "/adelanto@FinanzasBialyBot Juan 5000"})
        == "/adelanto Juan 5000"
    )
    # Un texto común con @ no se toca.
    assert telegram_api.extract_text({"text": "pago a juan@empresa"}) == "pago a juan@empresa"


def test_extract_media_refs_foto_toma_la_mayor_resolucion():
    msg = {"photo": [{"file_id": "chica"}, {"file_id": "grande"}]}
    assert telegram_api.extract_media_refs(msg) == [
        {"file_id": "grande", "mime": "image/jpeg"}
    ]


def test_extract_media_refs_documento_usa_su_mime():
    msg = {"document": {"file_id": "doc1", "mime_type": "application/pdf"}}
    assert telegram_api.extract_media_refs(msg) == [
        {"file_id": "doc1", "mime": "application/pdf"}
    ]


def test_extract_media_refs_documento_sin_mime_cae_al_default():
    msg = {"document": {"file_id": "doc1"}}
    assert telegram_api.extract_media_refs(msg)[0]["mime"] == "application/octet-stream"


def test_extract_media_refs_voz():
    msg = {"voice": {"file_id": "v1", "mime_type": "audio/ogg"}}
    assert telegram_api.extract_media_refs(msg) == [{"file_id": "v1", "mime": "audio/ogg"}]


def test_extract_media_refs_voz_sin_mime():
    assert telegram_api.extract_media_refs({"voice": {"file_id": "v1"}})[0]["mime"] == "audio/ogg"


def test_extract_media_refs_sin_adjunto():
    assert telegram_api.extract_media_refs({"text": "hola"}) == []


def test_split_message_no_parte_si_entra():
    assert telegram_api.split_message("corto") == ["corto"]


def test_split_message_corta_por_linea():
    linea = "x" * 1000
    texto = "\n".join([linea] * 6)  # ~6005 chars
    chunks = telegram_api.split_message(texto)
    assert len(chunks) > 1
    assert all(len(c) <= telegram_api.MAX_MESSAGE_LEN for c in chunks)
    # No se pierde contenido.
    assert "".join(chunks).replace("\n", "") == texto.replace("\n", "")


def test_split_message_parte_linea_mas_larga_que_el_limite():
    texto = "y" * (telegram_api.MAX_MESSAGE_LEN * 2 + 10)
    chunks = telegram_api.split_message(texto)
    assert all(len(c) <= telegram_api.MAX_MESSAGE_LEN for c in chunks)
    assert "".join(chunks) == texto
