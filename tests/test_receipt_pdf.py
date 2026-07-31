import ast
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

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

import app

PDF_RESULT = {
    "receipt": {"id": "r1", "number": "RP-000012", "receiptKind": "payment",
                "counterpartyName": "Distribuidora MGB"},
    "pdfUrl": "https://storage.invalid/signed/RP-000012.pdf",
    "fileName": "RP-000012_Distribuidora_MGB.pdf",
    "generated": True,
}


class _Recorder:
    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else {}
        self.raises = raises
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise self.raises
        return self.result


def _http_error(status: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://test.invalid/ingest")
    response = httpx.Response(status, json={"error": message}, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_todo_lo_que_se_usa_del_cliente_esta_importado():
    """Regresión: handle_receipt_pdf_query llamaba a receipt_pdf sin importarlo.

    Los tests no lo detectaron porque nadie ejecutaba ese handler, y el bot
    reventó recién en producción con NameError.
    """
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bialystok_client":
            imported |= {a.name for a in node.names}

    client_tree = ast.parse((ROOT / "bialystok_client.py").read_text(encoding="utf-8"))
    exported = {
        n.name for n in client_tree.body
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    }
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert (called & exported) - imported == set()


def test_pedido_de_recibo_devuelve_el_documento(monkeypatch):
    rec = _Recorder(PDF_RESULT)
    monkeypatch.setattr(app, "receipt_pdf", rec)
    replies = app.handle_receipt_pdf_query("+549", "mgb")
    assert len(replies) == 1
    doc = replies[0]
    assert doc["document"] == PDF_RESULT["pdfUrl"]
    assert doc["filename"] == "RP-000012_Distribuidora_MGB.pdf"
    assert "RP-000012" in doc["caption"]
    assert rec.calls[0][1]["search"] == "mgb"


def test_pedido_sin_texto_busca_el_ultimo(monkeypatch):
    rec = _Recorder(PDF_RESULT)
    monkeypatch.setattr(app, "receipt_pdf", rec)
    app.handle_receipt_pdf_query("+549", "")
    assert rec.calls[0][1]["search"] is None


def test_recibo_inexistente_devuelve_el_mensaje_del_backend(monkeypatch):
    monkeypatch.setattr(
        app, "receipt_pdf",
        _Recorder(raises=_http_error(404, 'No encontré ningún recibo que matchee "zzz".')),
    )
    replies = app.handle_receipt_pdf_query("+549", "zzz")
    assert "No encontré" in replies[0]


def test_error_de_red_no_explota(monkeypatch):
    monkeypatch.setattr(app, "receipt_pdf", _Recorder(raises=httpx.ConnectError("boom")))
    replies = app.handle_receipt_pdf_query("+549", "mgb")
    assert "No pude generar el recibo" in replies[0]


def test_build_receipt_document_devuelve_none_si_falla(monkeypatch):
    # Que falle el PDF no debe tumbar la respuesta del movimiento.
    monkeypatch.setattr(app, "receipt_pdf", _Recorder(raises=httpx.ConnectError("boom")))
    assert app.build_receipt_document("+549", movement_id="m1") is None


def test_build_receipt_document_sin_url_devuelve_none(monkeypatch):
    monkeypatch.setattr(app, "receipt_pdf", _Recorder({"receipt": {"number": "RP-1"}}))
    assert app.build_receipt_document("+549", movement_id="m1") is None


def test_flatten_replies_aplana_texto_y_documento():
    doc = {"document": "u", "filename": "f.pdf"}
    assert app._flatten_replies(["texto", ["otro", doc], None, ""]) == ["texto", "otro", doc]
