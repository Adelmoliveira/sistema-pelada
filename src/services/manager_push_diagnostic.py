"""Controlled Web Push transport diagnostic for the authenticated manager."""

import json
import os

from src.services.manager_push import (
    error_details,
    validate_subscription,
    vapid_configuration_error,
)


def send_diagnostic(subscription):
    """Attempt one push. Does not modify inventory or purchase alerts.

    Caller must enforce manager authorization, subscription ownership,
    CSRF, persistent rate limiting, and concurrent-request protection.
    """
    configuration_error = vapid_configuration_error()
    if configuration_error:
        return {"accepted": False, "code": configuration_error}

    try:
        endpoint, p256dh, auth = validate_subscription({
            "endpoint": subscription["endpoint"],
            "keys": {
                "p256dh": subscription["p256dh"],
                "auth": subscription["auth"],
            },
        })
    except (ValueError, KeyError, TypeError):
        return {"accepted": False, "code": "invalid_subscription"}

    from pywebpush import webpush

    payload = {
        "title": "GPCTA - Teste de Notificacao",
        "body": "Se voce recebeu esta mensagem, o envio push do GPCTA esta funcionando.",
        "url": "/stock/purchase-monitoring",
        "kind": "manager-push-diagnostic",
    }

    try:
        response = webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
            vapid_claims={
                "sub": os.environ.get(
                    "VAPID_SUBJECT", "mailto:diretoriagpcta@gmail.com"
                )
            },
            timeout=2,
            ttl=300,
        )
        status = getattr(response, "status_code", None)
        if status is not None and not 200 <= status < 300:
            return {
                "accepted": False,
                "code": "webpush_http_" + str(status),
            }
        return {"accepted": True, "code": "provider_accepted"}
    except Exception as exc:
        code, _permanent = error_details(exc)
        return {"accepted": False, "code": code}


def reserve_diagnostic(db, user_id):
    if not db.is_postgres:
        raise RuntimeError("Diagnostic rate limit requires PostgreSQL")

    sql = """
        INSERT INTO manager_push_diagnostic_control
            (user_id, last_attempt_at, updated_at)
        VALUES (%s, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET last_attempt_at = EXCLUDED.last_attempt_at,
            updated_at = EXCLUDED.updated_at
        WHERE manager_push_diagnostic_control.last_attempt_at
              <= NOW() - INTERVAL '60 seconds'
        RETURNING user_id
    """

    with db.conn.cursor() as cursor:
        cursor.execute(sql, (user_id,))
        return cursor.fetchone() is not None


def find_current_subscription(db, user_id, binding, device):
    """Find the active subscription owned by this manager and browser."""
    import hashlib
    import hmac

    if not binding or not device:
        return None

    if (
        len(device) != 64
        or any(char not in "0123456789abcdef" for char in device)
    ):
        return None

    binding_hash = hashlib.sha256(binding.encode()).hexdigest()
    device_hash = hashlib.sha256(device.encode()).hexdigest()

    user = db.execute(
        "SELECT role, active FROM users WHERE id=?",
        (user_id,),
    ).fetchone()

    if not user or user["role"] != "manager" or not user["active"]:
        return None

    revoked = db.execute(
        """
        SELECT 1
        FROM manager_push_revocations
        WHERE binding_hash IN (?, ?)
        LIMIT 1
        """,
        (binding_hash, "device:" + device_hash),
    ).fetchone()

    if revoked:
        return None

    rows = db.execute(
        """
        SELECT s.id, s.endpoint, s.p256dh, s.auth,
               s.binding_hash, s.device_hash
        FROM manager_push_subscriptions s
        WHERE s.user_id=?
          AND s.active=1
          AND s.revoked_at IS NULL
          AND s.binding_hash=?
          AND s.device_hash=?
          AND NOT EXISTS (
              SELECT 1 FROM push_subscriptions legacy
              WHERE legacy.endpoint=s.endpoint
          )
        LIMIT 2
        """,
        (user_id, binding_hash, device_hash),
    ).fetchall()

    if len(rows) != 1:
        return None

    subscription = dict(rows[0])

    if not hmac.compare_digest(
        subscription["binding_hash"], binding_hash
    ):
        return None

    if not hmac.compare_digest(
        subscription["device_hash"], device_hash
    ):
        return None

    return {
        "id": subscription["id"],
        "endpoint": subscription["endpoint"],
        "p256dh": subscription["p256dh"],
        "auth": subscription["auth"],
    }
