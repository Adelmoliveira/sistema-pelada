from src.services.email_reminders import send_gmail
from src.utils import brdate, cpfmask, money


def send_purchase_receipt(db, sale_id, sender, app_password, send_func=send_gmail):
    sale = db.execute(
        """SELECT s.*,p.name player_name,p.cpf,p.email
           FROM sales s JOIN players p ON p.id=s.player_id WHERE s.id=?""",
        (sale_id,),
    ).fetchone()
    if not sale or sale["receipt_sent_at"]:
        return "skipped"
    recipient = (sale["email"] or "").strip().lower()
    if "@" not in recipient:
        return "without_email"

    items = db.execute(
        """SELECT si.quantity,si.unit_price_cents,p.name
           FROM sale_items si JOIN products p ON p.id=si.product_id
           WHERE si.sale_id=? ORDER BY si.id""",
        (sale_id,),
    ).fetchall()
    purchase_time = sale["paid_at"] or sale["delivered_at"] or sale["created_at"]
    lines = [
        "Olá,",
        "",
        "Segue o comprovante da sua compra no PELADEIROS GPCTA.",
        "",
        f"Pedido: #{sale['id']}",
        f"Nome completo: {sale['player_name']}",
        f"CPF: {cpfmask(sale['cpf'])}",
        f"Data e horário: {brdate(purchase_time)}",
        "Estabelecimento: PELADEIROS GPCTA",
        f"Forma de pagamento: {sale['payment_method']}",
        "",
        "Produtos:",
    ]
    for item in items:
        subtotal = int(item["quantity"] or 0) * int(item["unit_price_cents"] or 0)
        lines.append(f"- {item['quantity']}x {item['name']} — {money(subtotal)}")
    lines.extend(["", f"Total pago: {money(sale['total_cents'])}", "", "Obrigado!"])
    try:
        send_func(sender, app_password, recipient, f"Comprovante de compra #{sale['id']} - PELADEIROS GPCTA", "\n".join(lines))
    except Exception as exc:
        db.execute("UPDATE sales SET receipt_error=? WHERE id=?", (str(exc)[:500], sale_id))
        db.commit()
        return "failed"
    db.execute("UPDATE sales SET receipt_sent_at=CURRENT_TIMESTAMP,receipt_error='' WHERE id=?", (sale_id,))
    db.commit()
    return "sent"
