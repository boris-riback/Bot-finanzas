import base64
import os
import threading
import time

import httpx

BASE_URL = os.environ["BIALYSTOK_INGEST_URL"]
TOKEN = os.environ["BOT_INGEST_TOKEN"]

_HEADERS = {
    "x-bot-token": TOKEN,
    "Content-Type": "application/json",
}

CATALOG_TTL_SECONDS = 3600

# El catálogo depende de la organización que resuelve el teléfono en el edge,
# así que se cachea por teléfono. Un slot único filtraría datos entre orgs.
_catalog_cache: dict[str, dict] = {}
_catalog_lock = threading.Lock()


def _post(payload: dict) -> dict:
    r = httpx.post(BASE_URL, json=payload, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_catalog(phone: str) -> dict:
    now = time.time()
    with _catalog_lock:
        entry = _catalog_cache.get(phone)
        if entry and entry["expires"] > now:
            return entry["data"]
    data = _post({"action": "catalog", "phone": phone})
    with _catalog_lock:
        _catalog_cache[phone] = {"data": data, "expires": now + CATALOG_TTL_SECONDS}
    return data


def upload_attachment(phone: str, origin_ref: str, mime: str, content: bytes) -> str:
    b64 = base64.b64encode(content).decode()
    resp = _post({
        "action": "upload_attachment",
        "phone": phone,
        "originRef": origin_ref,
        "mime": mime,
        "base64": b64,
    })
    return resp["path"]


def ingest(payload: dict) -> dict:
    return _post({"action": "ingest", **payload})


def list_pending(phone: str) -> dict:
    return _post({"action": "list_pending", "phone": phone})


def confirm_pending(phone: str, choice: dict, pending_id: str | None = None) -> dict:
    payload = {"action": "confirm_pending", "phone": phone, "choice": choice}
    if pending_id:
        payload["pendingId"] = pending_id
    return _post(payload)


def confirm_comprobante_pending(phone: str, pending_id: str, choice: dict) -> dict:
    return _post({
        "action": "confirm_comprobante_pending",
        "phone": phone,
        "pendingId": pending_id,
        "choice": choice,
    })


def fetch_summary(phone: str) -> dict:
    return _post({"action": "summary", "phone": phone})


def rrhh_advance(phone: str, origin_ref: str, employee_name: str, amount: float,
                 date: str | None = None, note: str | None = None) -> dict:
    payload = {
        "action": "rrhh_advance",
        "phone": phone,
        "originRef": origin_ref,
        "employeeName": employee_name,
        "amount": amount,
    }
    if date:
        payload["date"] = date
    if note:
        payload["note"] = note
    return _post(payload)


def rrhh_liquidate(phone: str, origin_ref: str, employee_name: str, reference_date: str) -> dict:
    return _post({
        "action": "rrhh_liquidate",
        "phone": phone,
        "originRef": origin_ref,
        "employeeName": employee_name,
        "referenceDate": reference_date,
    })


def rrhh_confirm_liquidation(phone: str, pending_id: str, confirm: bool) -> dict:
    return _post({
        "action": "rrhh_confirm_liquidation",
        "phone": phone,
        "pendingId": pending_id,
        "confirm": confirm,
    })


def internal_transfer(phone: str, origin_ref: str, from_cash_box: str, to_cash_box: str,
                      amount: float, transfer_date: str, reason: str | None = None,
                      notes: str | None = None) -> dict:
    payload = {
        "action": "internal_transfer",
        "phone": phone,
        "originRef": origin_ref,
        "fromCashBoxName": from_cash_box,
        "toCashBoxName": to_cash_box,
        "amount": amount,
        "transferDate": transfer_date,
    }
    if reason:
        payload["reason"] = reason
    if notes:
        payload["notes"] = notes
    return _post(payload)


def confirm_transfer_pending(phone: str, pending_id: str, choice: dict) -> dict:
    return _post({
        "action": "confirm_transfer_pending",
        "phone": phone,
        "pendingId": pending_id,
        "choice": choice,
    })


def request_cancel_movement(phone: str) -> dict:
    """Pide anular el último movimiento cargado por el bot.

    No anula nada: deja el pendiente esperando el SI y devuelve el resumen de lo
    que se anularía. El backend marca status='anulado' —el ERP nunca borra
    movimientos— y desimputa los comprobantes antes de hacerlo.
    """
    return _post({"action": "request_cancel_movement", "phone": phone})


def confirm_cancel_movement(phone: str, pending_id: str, confirm: bool) -> dict:
    return _post({
        "action": "confirm_cancel_movement",
        "phone": phone,
        "pendingId": pending_id,
        "confirm": confirm,
    })
