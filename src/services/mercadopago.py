"""Cliente mínimo da API de pagamentos Pix do Mercado Pago."""

import hashlib
import hmac
import json
import urllib.error
import urllib.request


API_URL = "https://api.mercadopago.com"


class MercadoPagoError(RuntimeError):
    pass


def _request(access_token, method, path, payload=None, idempotency_key=None):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(API_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("message") or detail.get("error") or str(detail)
        except Exception:
            message = str(exc)
        raise MercadoPagoError(f"Mercado Pago recusou a solicitação: {message}") from exc
    except urllib.error.URLError as exc:
        raise MercadoPagoError("Não foi possível conectar ao Mercado Pago.") from exc


def create_pix_payment(access_token, amount_cents, description, payer, reference,
                       idempotency_key, notification_url=None):
    payload = {
        "transaction_amount": amount_cents / 100,
        "description": description,
        "payment_method_id": "pix",
        "external_reference": reference,
        "payer": payer,
    }
    if notification_url:
        payload["notification_url"] = notification_url
    return _request(access_token, "POST", "/v1/payments", payload, idempotency_key)


def get_payment(access_token, payment_id):
    return _request(access_token, "GET", f"/v1/payments/{payment_id}")


def validate_webhook_signature(x_signature, x_request_id, data_id, secret):
    """Valida o manifesto HMAC documentado pelo Mercado Pago."""
    if not all((x_signature, x_request_id, data_id, secret)):
        return False
    parts = {}
    for item in x_signature.split(","):
        key, separator, value = item.partition("=")
        if separator:
            parts[key.strip()] = value.strip()
    timestamp = parts.get("ts")
    received = parts.get("v1")
    if not timestamp or not received:
        return False
    manifest = f"id:{str(data_id).lower()};request-id:{x_request_id};ts:{timestamp};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
