import hashlib
import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "https://api.mercadopago.com"


class MercadoPagoError(RuntimeError):
    pass


def _request(method, path, access_token, payload=None, idempotency_key=None):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(f"{API_BASE}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            details = json.loads(exc.read().decode("utf-8"))
            message = details.get("message") or details.get("error") or str(details)
        except Exception:
            message = f"HTTP {exc.code}"
        raise MercadoPagoError(f"Mercado Pago recusou a solicitação: {message}") from exc
    except (URLError, TimeoutError) as exc:
        raise MercadoPagoError("Não foi possível conectar ao Mercado Pago.") from exc


def create_qr_order(access_token, external_pos_id, external_reference, amount_cents, idempotency_key, items):
    amount = f"{amount_cents / 100:.2f}"
    payload = {
        "type": "qr",
        "total_amount": amount,
        "description": "Consumo Bar Peladeiros",
        "external_reference": external_reference,
        "expiration_time": "PT15M",
        "config": {"qr": {"external_pos_id": external_pos_id, "mode": "dynamic"}},
        "transactions": {"payments": [{"amount": amount}]},
        "items": [
            {
                "title": item["name"][:100],
                "unit_price": f"{item['unit_price_cents'] / 100:.2f}",
                "unit_measure": "unit",
                "external_code": str(item["product_id"]),
                "quantity": item["quantity"],
            }
            for item in items
        ],
    }
    return _request("POST", "/v1/orders", access_token, payload, idempotency_key)


def get_order(access_token, order_id):
    return _request("GET", f"/v1/orders/{order_id}", access_token)


def validate_webhook_signature(x_signature, x_request_id, data_id, secret):
    if not all((x_signature, x_request_id, data_id, secret)):
        return False
    parts = {}
    for part in x_signature.split(","):
        key, separator, value = part.strip().partition("=")
        if separator:
            parts[key] = value
    timestamp = parts.get("ts")
    received = parts.get("v1")
    if not timestamp or not received:
        return False
    template = f"id:{data_id.lower()};request-id:{x_request_id};ts:{timestamp};"
    expected = hmac.new(secret.encode("utf-8"), template.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
