import json
import os
from datetime import datetime, timedelta

from flask import current_app

from src.services.purchase_receipts import send_delivery_update, send_purchase_receipt
from src.services.push_notifications import send_player_push_once

OUTBOX_BATCH_SIZE = 50
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_EVENT_TYPES = (
    "delivery_push",
    "delivery_update_email",
    "purchase_receipt_email",
)


def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _backoff_seconds(attempts):
    return min(60 * (2 ** max(0, attempts - 1)), 900)


def _homologation_mode():
    return (os.environ.get("APP_ENV") or "").strip().lower() == "homologation"


def enqueue_delivery_events(db, sale_id, delivery_id, payload):
    """Persist delivery notifications in a local transactional outbox."""
    inserted = 0
    for event_type in OUTBOX_EVENT_TYPES:
        event_key = f"delivery:{delivery_id}:{event_type}"
        row_payload = payload.get(event_type, {}) if isinstance(payload, dict) else {}
        cursor = db.execute(
            """
            INSERT INTO notification_outbox(
                event_key, event_type, sale_id, delivery_id, payload,
                status, attempts, available_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT(event_key) DO NOTHING
            """,
            (
                event_key,
                event_type,
                int(sale_id),
                int(delivery_id),
                json.dumps(row_payload, sort_keys=True),
            ),
        )
        if getattr(cursor, "rowcount", 0) != 0:
            inserted += 1
    return inserted


def _select_pending_events(db, batch_size=OUTBOX_BATCH_SIZE):
    now = _now_iso()
    if getattr(db, "is_postgres", False):
        return db.execute(
            """
            SELECT * FROM notification_outbox
            WHERE status='pending'
              AND attempts < ?
              AND available_at <= CURRENT_TIMESTAMP
            ORDER BY id
            LIMIT ?
            FOR UPDATE SKIP LOCKED
            """,
            (OUTBOX_MAX_ATTEMPTS, int(batch_size)),
        ).fetchall()
    return db.execute(
        """
        SELECT * FROM notification_outbox
        WHERE status='pending'
          AND attempts < ?
          AND available_at <= ?
        ORDER BY id
        LIMIT ?
        """,
        (OUTBOX_MAX_ATTEMPTS, now, int(batch_size)),
    ).fetchall()


def _mark_processing(db, event_id, attempts):
    db.execute(
        """
        UPDATE notification_outbox
        SET status='processing', attempts=?, processing_started_at=CURRENT_TIMESTAMP,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (attempts, event_id),
    )


def _mark_sent(db, event_id):
    db.execute(
        """
        UPDATE notification_outbox
        SET status='sent', processed_at=CURRENT_TIMESTAMP, last_error='', updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (event_id,),
    )


def _mark_retry(db, event_id, attempts, error_message):
    next_attempt = int(attempts or 0)
    if next_attempt >= OUTBOX_MAX_ATTEMPTS:
        db.execute(
            """
            UPDATE notification_outbox
            SET status='failed', processed_at=CURRENT_TIMESTAMP, last_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (error_message[:500], event_id),
        )
        return False
    retry_at = datetime.utcnow() + timedelta(seconds=_backoff_seconds(next_attempt + 1))
    db.execute(
        """
        UPDATE notification_outbox
        SET status='pending', attempts=?, available_at=?, last_error=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (next_attempt, retry_at.strftime("%Y-%m-%d %H:%M:%S"), error_message[:500], event_id),
    )
    return True


def _dispatch_event(db, event):
    payload = json.loads(event["payload"] or "{}")
    event_type = event["event_type"]
    if event_type == "delivery_push":
        result = send_player_push_once(
            db,
            int(payload["player_id"]),
            payload["kind"],
            payload["period"],
            payload.get("title", ""),
            payload.get("body", ""),
            payload.get("url", "/"),
            payload.get("image_url", ""),
            payload.get("include_push_image", True),
            payload.get("declarative", False),
            payload.get("body_html", ""),
        )
        if result.get("sent", 0) or result.get("skipped", 0):
            return True
        return False
    if event_type == "delivery_update_email":
        sender = current_app.config.get("GMAIL_SMTP_USER", "")
        password = current_app.config.get("GMAIL_APP_PASSWORD", "")
        result = send_delivery_update(
            db,
            int(payload["sale_id"]),
            payload.get("delivered_items", []),
            payload.get("remaining_items", []),
            sender,
            password,
        )
        if result in {"skipped", "without_email", "sent"}:
            return True
        return False
    if event_type == "purchase_receipt_email":
        sender = current_app.config.get("GMAIL_SMTP_USER", "")
        password = current_app.config.get("GMAIL_APP_PASSWORD", "")
        result = send_purchase_receipt(
            db,
            int(payload["sale_id"]),
            sender,
            password,
        )
        if result in {"skipped", "without_email", "sent"}:
            return True
        return False
    raise ValueError(f"Tipo de evento desconhecido: {event_type}")


def process_notification_outbox(db, batch_size=OUTBOX_BATCH_SIZE):
    """Process notifications queued for asynchronous delivery."""
    if _homologation_mode():
        rows = db.execute(
            "SELECT * FROM notification_outbox WHERE status='pending' ORDER BY id LIMIT ?",
            (int(batch_size),),
        ).fetchall()
        processed = 0
        for event in rows:
            db.execute(
                "UPDATE notification_outbox SET status='sent', processed_at=CURRENT_TIMESTAMP, last_error='APP_ENV=homologation', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (event["id"],),
            )
            processed += 1
        db.commit()
        return {"processed": processed, "sent": processed, "failed": 0, "skipped_homologation": processed}

    pending = _select_pending_events(db, batch_size)
    processed = 0
    sent = 0
    failed = 0
    for event in pending:
        attempts = int(event["attempts"] or 0) + 1
        _mark_processing(db, event["id"], attempts)
        try:
            ok = _dispatch_event(db, event)
        except Exception as exc:
            ok = False
            error_message = f"{type(exc).__name__}: {exc}"
            if _mark_retry(db, event["id"], attempts, error_message):
                continue
            failed += 1
            processed += 1
            continue
        if ok:
            _mark_sent(db, event["id"])
            sent += 1
        else:
            error_message = "Dispatch reportou falha de entrega."
            if _mark_retry(db, event["id"], attempts, error_message):
                continue
            failed += 1
        processed += 1
    db.commit()
    return {"processed": processed, "sent": sent, "failed": failed}
