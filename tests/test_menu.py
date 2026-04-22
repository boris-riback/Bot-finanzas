import os
import sys
import pathlib

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_test")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test")
os.environ.setdefault("TWILIO_WHATSAPP_NUMBER", "+100000000")
os.environ.setdefault("BIALYSTOK_INGEST_URL", "https://test.invalid/ingest")
os.environ.setdefault("BOT_INGEST_TOKEN", "test")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import _build_menu_indexes, format_candidate_menu


CANDIDATES = [{"id": "a", "name": "Juan Pérez SA"}, {"id": "b", "name": "Juanita Pérez"}]


def test_menu_egreso_uses_proveedor():
    text = format_candidate_menu(CANDIDATES, "Juan Perez", "", kind="egreso")
    assert "proveedor" in text.lower()
    assert "cliente" not in text.lower()
    assert "Proveedor Varios" in text
    assert "Dejar en blanco" in text


def test_menu_ingreso_uses_cliente():
    text = format_candidate_menu(CANDIDATES, "Juan Perez", "", kind="ingreso")
    assert "cliente" in text.lower()
    assert "proveedor" not in text.lower()
    assert "Cliente Varios" in text
    assert "Dejar en blanco" in text


def test_menu_default_is_egreso():
    text = format_candidate_menu(CANDIDATES, "X", "")
    assert "proveedor" in text.lower()


def test_menu_without_raw_name_hides_crear_nuevo():
    text = format_candidate_menu(CANDIDATES, "", "", kind="egreso")
    assert "Crear nuevo" not in text
    assert "Dejar en blanco" in text
    assert "Cancelar" in text


def test_menu_with_cbu_only():
    text = format_candidate_menu([], "", "0000000000", kind="egreso")
    assert "0000000000" in text
    assert "Dejar en blanco" in text


def test_indexes_with_raw_name():
    idx = _build_menu_indexes(CANDIDATES, "Juan Perez")
    assert idx["visible_count"] == 2
    assert idx["varios"] == 3
    assert idx["create"] == 4
    assert idx["skip"] == 5
    assert idx["cancel"] == 6


def test_indexes_without_raw_name():
    idx = _build_menu_indexes(CANDIDATES, "")
    assert idx["visible_count"] == 2
    assert idx["varios"] == 3
    assert idx["create"] is None
    assert idx["skip"] == 4
    assert idx["cancel"] == 5


def test_indexes_truncated_candidates():
    # More than 5 candidates → only top 5 visible
    many = [{"id": f"id{i}", "name": f"CP {i}"} for i in range(8)]
    idx = _build_menu_indexes(many, "foo")
    assert idx["visible_count"] == 5
    assert idx["varios"] == 6
    assert idx["create"] == 7
    assert idx["skip"] == 8
    assert idx["cancel"] == 9


def test_indexes_empty_candidates_no_raw_name():
    idx = _build_menu_indexes([], "")
    assert idx["visible_count"] == 0
    assert idx["varios"] == 1
    assert idx["create"] is None
    assert idx["skip"] == 2
    assert idx["cancel"] == 3
