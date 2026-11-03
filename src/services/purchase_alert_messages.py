"""Persistent manager messages created independently from Web Push delivery."""

from src.services.purchase_observation import _insert, _now


MESSAGE_URL = "/stock/purchase-planning"
KIND_LABELS = {
    "opened": "Nova necessidade",
    "worsened": "Agravamento",
}


def create_alert_messages(db, alert_ids):
    """Create one immutable alert message per currently active manager."""
    ids = sorted({int(alert_id) for alert_id in alert_ids})
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    alerts = db.execute(
        """SELECT a.id,a.kind,a.suggested_units,p.name product_name
           FROM purchase_stock_alerts a
           JOIN purchase_replenishment_episodes e ON e.id=a.episode_id
           JOIN products p ON p.id=e.product_id
           WHERE a.id IN (""" + marks + ") ORDER BY a.id",
        tuple(ids),
    ).fetchall()
    managers = db.execute(
        "SELECT id FROM users WHERE role='manager' AND active=1 ORDER BY id"
    ).fetchall()
    now = _now()
    messages = []
    for alert in alerts:
        kind = KIND_LABELS[alert["kind"]]
        product = " ".join(str(alert["product_name"] or "Produto").split())
        quantity = max(0, int(alert["suggested_units"] or 0))
        title = f"{kind}: {product}"
        body = (f"Produto: {product}\nTipo: {kind}\n"
                f"Quantidade sugerida: {quantity} unidades.")
        messages.extend(
            (alert["id"], manager["id"], title, body, MESSAGE_URL, now)
            for manager in managers
        )
    _insert(
        db,
        "purchase_alert_messages",
        ("alert_id", "user_id", "title", "body", "url", "created_at"),
        messages,
        "ON CONFLICT(alert_id,user_id) DO NOTHING",
    )
    return len(messages)
