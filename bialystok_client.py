import base64
import os
import time

import httpx

BASE_URL = os.environ["BIALYSTOK_INGEST_URL"]
TOKEN = os.environ["BOT_INGEST_TOKEN"]

_HEADERS = {
    "x-bot-token": TOKEN,
    "Content-Type": "application/json",
}

_catalog_cache = {"data": None, "expires": 0}


def _post(payload: dict) -> dict:
    r = httpx.post(BASE_URL, json=payload, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_catalog(phone: str) -> dict:
    now = time.time()
    if _catalog_cache["data"] and _catalog_cache["expires"] > now:
        return _catalog_cache["data"]
    data = _post({"action": "catalog", "phone": phone})
    _catalog_cache.update(data=data, expires=now + 3600)
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
