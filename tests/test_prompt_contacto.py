"""Datos de contacto de la contraparte en el prompt.

Lo que se cuida acá es que el modelo nunca confunda a la empresa propia con el
proveedor: en una factura de compra hay dos CUIT y el equivocado termina cargado
en la ficha del proveedor, que es justo el dato con el que después se le paga.
"""
import os
import pathlib
import sys

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

ORG = {"id": "org-1", "name": "Bialystok", "legalName": "Bialystok SRL", "taxId": "30712345678"}
CATALOG = {"organization": ORG, "counterparties": [], "businessUnits": []}


def test_bloque_de_empresa_propia_con_todos_los_datos():
    block = app.build_own_company_block(ORG)

    assert "Bialystok SRL" in block
    assert "30712345678" in block
    assert "NUNCA es la contraparte" in block


def test_bloque_vacio_si_no_hay_organizacion():
    assert app.build_own_company_block(None) == ""
    assert app.build_own_company_block({}) == ""
    assert app.build_own_company_block({"id": "org-1"}) == ""


def test_bloque_omite_los_campos_que_faltan():
    block = app.build_own_company_block({"name": "Bialystok"})

    assert "Bialystok" in block
    assert "CUIT" not in block
    assert "razón social" not in block


def test_el_prompt_pide_los_datos_de_contacto():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    for field in ("counterpartyTaxId", "counterpartyEmail", "counterpartyPhone",
                  "counterpartyIvaCondition", "counterpartyAddress"):
        assert field in prompt


def test_el_prompt_explica_de_quien_son_los_datos():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "NUNCA de la empresa que usa el sistema" in prompt
    assert "quien EMITE la factura" in prompt
    assert "DESTINATARIO" in prompt


def test_ante_la_duda_el_prompt_pide_null():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "devolvé null" in prompt
    assert "ensucia la ficha del proveedor" in prompt


def test_la_condicion_de_iva_no_se_deduce():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "NO la deduzcas de la letra de la factura" in prompt


def test_el_prompt_distingue_factura_de_pago():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "documentKind" in prompt
    # Las señales que separan una factura de una constancia de transferencia.
    assert "CAE" in prompt
    assert "número de operación" in prompt
    assert "una factura impaga es una deuda" in prompt.lower()


def test_sin_adjunto_el_movimiento_es_un_pago():
    prompt = app.build_prompt_text(CATALOG, "MGB 5000 efectivo", has_attachment=False)

    assert 'devolvé "pago"' in prompt


def test_el_prompt_pide_el_iva_discriminado():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "taxVatLines" in prompt
    assert "taxOtherAmount" in prompt
    # El riesgo es que invente el IVA en vez de copiarlo.
    assert "NUNCA calcules el IVA" in prompt
    assert "10.5" in prompt


def test_el_total_sigue_siendo_el_total():
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "No lo cambies por el neto" in prompt


def test_el_prompt_sigue_pidiendo_los_alias():
    # El aprendizaje de alias depende de este campo: si se cae, el importador
    # deja de aprender y vuelve a preguntar por el mismo proveedor siempre.
    prompt = app.build_prompt_text(CATALOG, "", has_attachment=True)

    assert "counterpartyAliasHints" in prompt
