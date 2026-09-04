"""ZIP del home banking: un mensaje con el paquete carga la tanda entera.

Banco Galicia entrega la emisión de e-cheqs comprimida, con un resumen en la
raíz y un PDF por cheque en `detalles/`. Lo que se protege acá es que el resumen
NO entre como movimiento: cargarlo sumaría un pago por el total de la tanda
encima de los cheques que ya entraron uno por uno.
"""
import io
import os
import pathlib
import sys
import zipfile

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


def make_zip(files: dict) -> dict:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return {"mime": "application/zip", "bytes": buffer.getvalue()}


GALICIA = {
    "Detalles_operacion_nroLR93D7Q164.pdf": b"%PDF resumen de la tanda",
    "detalles/Cheque269_FIBO DE LUJAN S.R.L..._33717856789.pdf": b"%PDF cheque 269",
    "detalles/Cheque270_FIBO DE LUJAN S.R.L..._33717856789.pdf": b"%PDF cheque 270",
}


def test_deja_afuera_el_resumen_y_devuelve_un_adjunto_por_cheque():
    items, notes = app.expand_zip_items([make_zip(GALICIA)])

    assert notes == []
    assert [item["bytes"] for item in items] == [b"%PDF cheque 269", b"%PDF cheque 270"]
    assert {item["mime"] for item in items} == {"application/pdf"}


def test_ordena_los_cheques_por_numero():
    items, _ = app.expand_zip_items([make_zip({
        "detalles/Cheque10_X.pdf": b"diez",
        "detalles/Cheque2_X.pdf": b"dos",
    })])

    assert [item["bytes"] for item in items] == [b"dos", b"diez"]


def test_toma_todos_los_pdf_cuando_no_hay_subcarpeta():
    items, _ = app.expand_zip_items([make_zip({"a.pdf": b"uno", "b.pdf": b"dos"})])

    assert len(items) == 2


def test_ignora_la_basura_del_compresor():
    items, _ = app.expand_zip_items([make_zip({
        "__MACOSX/detalles/._Cheque269.pdf": b"basura",
        "detalles/.DS_Store": b"basura",
        "detalles/resumen.xlsx": b"no es pdf",
        "detalles/Cheque269.pdf": b"%PDF cheque",
    })])

    assert [item["bytes"] for item in items] == [b"%PDF cheque"]


def test_los_adjuntos_que_no_son_zip_pasan_derecho():
    pdf = {"mime": "application/pdf", "bytes": b"%PDF suelto"}
    items, notes = app.expand_zip_items([pdf, make_zip(GALICIA)])

    assert notes == []
    assert items[0] is pdf
    assert len(items) == 3


def test_avisa_cuando_el_zip_no_trae_comprobantes():
    items, notes = app.expand_zip_items([make_zip({"leeme.txt": b"nada"})])

    assert items == []
    assert "no tiene ningún comprobante" in notes[0].lower()


def test_avisa_cuando_el_zip_esta_roto_sin_perder_los_demas_adjuntos():
    pdf = {"mime": "application/pdf", "bytes": b"%PDF suelto"}
    roto = {"mime": "application/zip", "bytes": b"esto no es un zip"}
    items, notes = app.expand_zip_items([pdf, roto])

    assert items == [pdf]
    assert "no pude abrir el zip" in notes[0].lower()


def test_corta_una_emision_mas_grande_que_el_techo():
    grande = {f"detalles/Cheque{i}.pdf": b"%PDF" for i in range(app.MAX_ZIP_ENTRIES + 1)}
    items, notes = app.expand_zip_items([make_zip(grande)])

    assert items == []
    assert "máximo" in notes[0]
