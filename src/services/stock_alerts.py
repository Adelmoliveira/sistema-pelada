import os

from src.services.email_reminders import send_gmail


def _supplier_email(product):
    email = str(product["supplier_email"] or "").strip().lower()
    return email if "@" in email else ""


def notify_low_stock(db, product_ids=None, send_func=send_gmail):
    """Send one consolidated low-stock message per supplier.

    Every active low-stock product is considered. A state row prevents repeat
    messages until that product is replenished above its configured limit.
    """
    result = {"sent": 0, "skipped": 0, "without_supplier": 0, "failed": 0}
    products = db.execute(
        """SELECT id,name,stock,min_stock,supplier_email,active FROM products
           WHERE active=1 AND stock<=min_stock ORDER BY LOWER(name),name"""
    ).fetchall()
    groups = {}
    for product in products:
        supplier = _supplier_email(product)
        if not supplier:
            result["without_supplier"] += 1
            continue
        state = db.execute("SELECT alerted FROM stock_alert_states WHERE product_id=?", (product["id"],)).fetchone()
        if state and state["alerted"]:
            result["skipped"] += 1
            continue
        groups.setdefault(supplier, []).append(product)

    sender = os.environ.get("GMAIL_SMTP_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    for supplier, supplier_products in groups.items():
        lines = ["Olá,", "", "Os seguintes produtos atingiram o nível mínimo de estoque:", ""]
        for product in supplier_products:
            lines.append(f"- {product['name']}: {product['stock']} unidade(s) (limite: {product['min_stock']})")
        lines.extend(["", "Verifique a necessidade de reposição.", "", "PELADEIROS GPCTA"])
        try:
            if not sender or not password:
                result["failed"] += 1
                continue
            send_func(sender, password, supplier, "Alerta de estoque baixo - PELADEIROS GPCTA", "\n".join(lines))
            for product in supplier_products:
                db.execute(
                    """INSERT INTO stock_alert_states(product_id,alerted,last_stock,last_notified_at)
                       VALUES(?,1,?,CURRENT_TIMESTAMP)
                       ON CONFLICT(product_id) DO UPDATE SET alerted=1,last_stock=excluded.last_stock,
                       last_notified_at=CURRENT_TIMESTAMP""",
                    (product["id"], product["stock"]),
                )
            result["sent"] += 1
        except Exception:
            result["failed"] += 1

    db.execute(
        """UPDATE stock_alert_states SET alerted=0,last_stock=(SELECT p.stock FROM products p WHERE p.id=stock_alert_states.product_id)
           WHERE alerted=1 AND EXISTS (SELECT 1 FROM products p WHERE p.id=stock_alert_states.product_id AND p.stock>p.min_stock)"""
    )
    db.commit()
    return result
