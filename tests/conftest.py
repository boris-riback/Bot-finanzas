"""Helpers para armar la respuesta de list_pending en los tests.

El backend devuelve las preguntas abiertas como una sola cola cronológica: `next`
es la más vieja y es la única que una respuesta numérica puede resolver. Las
claves viejas (pending / pendingComprobante / pendingTransfer) siguen viajando
con la más vieja de cada tabla.
"""

PENDING_KEY_BY_KIND = {
    "counterparty": "pending",
    "comprobante": "pendingComprobante",
    "transfer": "pendingTransfer",
}


def make_prompt(kind: str, row: dict, subject: dict | None = None) -> dict:
    return {
        "kind": kind,
        "id": row["id"],
        "createdAt": None,
        "expiresAt": None,
        "position": 1,
        "total": 1,
        "subject": subject or {"counterpartyName": None, "amount": None, "movementKind": "egreso"},
        "data": row,
    }


def pending_response(*prompts: dict) -> dict:
    """Respuesta de list_pending con la cola en el orden en que se pasan."""
    ordered = [{**prompt, "position": i + 1, "total": len(prompts)} for i, prompt in enumerate(prompts)]
    response = {
        "pending": None,
        "pendingComprobante": None,
        "pendingTransfer": None,
        "pendingLiquidation": None,
        "pendingCancellation": None,
        "next": ordered[0] if ordered else None,
        "queue": ordered,
    }
    for prompt in ordered:
        key = PENDING_KEY_BY_KIND[prompt["kind"]]
        if response[key] is None:
            response[key] = prompt["data"]
    return response
