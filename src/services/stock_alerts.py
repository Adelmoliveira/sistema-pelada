import os

from src.services.email_reminders import send_gmail


def _recipients(product):
    values = [
        product["supplier_email"] if product and "supplier_email" in product.keys() else "",
        os.environ.get("STOCK_ALERT_ATTENDANT_EMAIL", ""),
        os.environ.get("STOCK_ALERT_MANAGER_EMAIL", ""),
    ]
    recipients = []
    for value in values:
        for email in str(value or "").replace(";", ",").split(","):
            email = email.strip().lower()
            if email and "@" in email and email not in recipients:
                recipients.append(email)
    return recipients


def notify_low_stock(db, product_ids, send_func=send_gmail):
    """Notify configured recipients when products cross their low-stock limit.

    A state row prevents repeated messages while the product remains below the
    limit. A later replenishment above the limit resets that state.
    """
    result = {"sent": 0, "skipped": 0, "without_recipients": 0, "failed": 0}
    for product_id in set(product_ids or []):
        product = db.execute(
            "SELECT id,name,stock,min_stock,supplier_email,active FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
        if not product or not product["active"]:
            continue
        state = db.execute(
            "SELECT alerted FROM stock_alert_states WHERE product_id=?", (product_id,)
        ).fetchone()
        if product["stock"] > product["min_stock"]:
            db.execute(
                "INSERT INTO stock_alert_states(product_id,alerted,last_stock) VALUES(?,0,?) "
                "ON CONFLICT(product_id) DO UPDATE SET alerted=0,last_stock=excluded.last_stock",
                (product_id, product["stock"]),
            )
            continue
        if state and state["alerted"]:
            result["skipped"] += 1
            db.execute(
                "UPDATE stock_alert_states SET last_stock=? WHERE product_id=?",
                (product["stock"], product_id),
            )
            continue
        recipients = _recipients(product)
        if not recipients:
            result["without_recipients"] += 1
            continue
        subject = f"Estoque baixo: {product['name']}"
        body = (
            "Olá,\n\n"
            f"O produto {product['name']} atingiu o nível mínimo de estoque.\n\n"
            f"Quantidade atual: {product['stock']} unidade(s)\n"
            f"Limite configurado: {product['min_stock']} unidade(s)\n\n"
            "Verifique a necessidade de reposição.\n\n"
            "PELADEIROS GPCTA"
        )
        sender = os.environ.get("GMAIL_SMTP_USER", "")
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        try:
            if not sender or not password:
                result["without_recipients"] += 1
                continue
            for recipient in recipients:
                send_func(sender, password, recipient, subject, body)
            db.execute(
                """INSERT INTO stock_alert_states(product_id,alerted,last_stock,last_notified_at)
                   VALUES(?,1,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(product_id) DO UPDATE SET alerted=1,last_stock=excluded.last_stock,
                   last_notified_at=CURRENT_TIMESTAMP""",
                (product_id, product["stock"]),
            )
            result["sent"] += len(recipients)
        except Exception:
            result["failed"] += 1
    db.commit()
    return result
