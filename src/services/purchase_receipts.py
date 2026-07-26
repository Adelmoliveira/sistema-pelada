import html

from src.services.email_reminders import send_gmail_html
from src.utils import brdate, cpfmask, money


def send_purchase_receipt(db, sale_id, sender, app_password, send_func=send_gmail_html):
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
    plain_body = "\n".join(lines)
    esc = html.escape
    html_items = "".join(
        f"<tr><td style='padding:8px 0;border-bottom:1px solid #e5edf2'>{item['quantity']}x {esc(item['name'])}</td>"
        f"<td style='padding:8px 0;border-bottom:1px solid #e5edf2;text-align:right'>{money(int(item['quantity'] or 0) * int(item['unit_price_cents'] or 0))}</td></tr>"
        for item in items
    )
    html_body = f"""<div style="margin:0;background:#f2f6f9;padding:24px;font-family:Arial,sans-serif;color:#183247">
      <div style="max-width:620px;margin:auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 12px #1232">
        <div style="background:#07558c;padding:20px;text-align:center"><img src="https://sistema-pelada-one.vercel.app/static/logo-gpcta.jpeg" alt="Logo GPCTA" style="max-width:110px;max-height:90px;object-fit:contain"><h1 style="color:#fff;font-size:22px;margin:10px 0 0">PELADEIROS GPCTA</h1></div>
        <div style="padding:24px"><h2 style="margin-top:0;color:#07558c">Comprovante de compra</h2><p>Olá, <strong>{esc(sale['player_name'])}</strong>!</p>
        <p>Obrigado pela sua compra. Confira os dados do pedido:</p>
        <p><strong>Pedido:</strong> #{sale['id']}<br><strong>CPF:</strong> {esc(cpfmask(sale['cpf']))}<br><strong>Data e horário:</strong> {esc(brdate(purchase_time))}<br><strong>Estabelecimento:</strong> PELADEIROS GPCTA<br><strong>Pagamento:</strong> {esc(sale['payment_method'])}</p>
        <h3 style="border-bottom:2px solid #07558c;padding-bottom:8px">Produtos</h3><table style="width:100%;border-collapse:collapse">{html_items}</table>
        <p style="font-size:20px;text-align:right"><strong>Total pago: {money(sale['total_cents'])}</strong></p><p style="color:#607d8b">Guarde este e-mail como comprovante da compra.</p></div></div></div>"""
    try:
        send_func(sender, app_password, recipient, f"Comprovante de compra #{sale['id']} - PELADEIROS GPCTA", plain_body, html_body)
    except Exception as exc:
        db.execute("UPDATE sales SET receipt_error=? WHERE id=?", (str(exc)[:500], sale_id))
        db.commit()
        return "failed"
    db.execute("UPDATE sales SET receipt_sent_at=CURRENT_TIMESTAMP,receipt_error='' WHERE id=?", (sale_id,))
    db.commit()
    return "sent"


def send_delivery_update(db, sale_id, delivered_items, remaining_items, sender, app_password, send_func=send_gmail_html):
    """Envia ao peladeiro o resumo de uma retirada parcial ou total."""
    sale = db.execute(
        "SELECT s.id,s.player_id,s.payment_method,p.email,p.name player_name FROM sales s JOIN players p ON p.id=s.player_id WHERE s.id=?",
        (sale_id,),
    ).fetchone()
    if not sale or "@" not in (sale["email"] or "").strip():
        return "without_email"
    delivered_lines = ", ".join(f"{item['quantity']}x {item['name']}" for item in delivered_items)
    remaining_lines = ", ".join(f"{item['quantity']}x {item['name']}" for item in remaining_items) or "Nenhum item pendente"
    fully_delivered = not remaining_items
    lines = [
        "Olá,", "", f"Atualização da retirada do pedido #{sale_id} no PELADEIROS GPCTA.", "",
        f"Itens retirados agora: {delivered_lines}.",
        f"Itens restantes: {remaining_lines}.", "",
        "Pedido totalmente entregue. Não há itens pendentes." if fully_delivered else "Você poderá retirar os itens restantes em outra ocasião.",
    ]
    plain_body = "\n".join(lines)
    esc = html.escape
    delivered_html = "".join(f"<li>{item['quantity']}x {esc(item['name'])}</li>" for item in delivered_items)
    remaining_html = "".join(f"<li>{item['quantity']}x {esc(item['name'])}</li>" for item in remaining_items) or "<li>Nenhum item pendente</li>"
    status_text = "Pedido totalmente entregue. Não há itens pendentes." if fully_delivered else "Os itens restantes poderão ser retirados posteriormente."
    html_body = f"""<div style='font-family:Arial,sans-serif;color:#183247;line-height:1.5'>
      <div style='background:#07558c;padding:18px;text-align:center'><img src='https://sistema-pelada-one.vercel.app/static/logo-gpcta.jpeg' alt='Logo GPCTA' style='max-width:100px'><h2 style='color:#fff;margin:8px 0 0'>PELADEIROS GPCTA</h2></div>
      <div style='padding:20px'><p>Olá, <strong>{esc(sale['player_name'])}</strong>!</p><p>Atualização da retirada do pedido <strong>#{sale_id}</strong>:</p>
      <p><strong>Retirados agora:</strong></p><ul>{delivered_html}</ul><p><strong>Restantes:</strong></p><ul>{remaining_html}</ul><p>{status_text}</p></div></div>"""
    try:
        send_func(sender, app_password, (sale["email"] or "").strip().lower(), f"Atualização do pedido #{sale_id} - PELADEIROS GPCTA", plain_body, html_body)
    except Exception as exc:
        current_error = str(exc)[:500]
        db.execute("UPDATE sales SET receipt_error=? WHERE id=?", (current_error, sale_id))
        db.commit()
        return "failed"
    return "sent"
