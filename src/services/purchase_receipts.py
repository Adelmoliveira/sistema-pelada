import html

from src.services.email_reminders import send_gmail_html
from src.utils import brdate, cpfmask, money


def send_purchase_receipt(db, sale_id, sender, app_password, send_func=send_gmail_html):
    sale = db.execute(
        """SELECT s.*,COALESCE(p.name,s.guest_name,'Convidado') player_name,p.cpf,p.email
           FROM sales s LEFT JOIN players p ON p.id=s.player_id WHERE s.id=?""",
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
        """SELECT s.id,s.player_id,s.payment_method,s.total_cents,s.paid_at,s.created_at,
                  s.delivered_at,p.email,COALESCE(p.name,s.guest_name,'Convidado') player_name,p.cpf,
                  (SELECT MAX(sid.delivered_at) FROM sale_item_deliveries sid
                   JOIN sale_items si2 ON si2.id=sid.sale_item_id WHERE si2.sale_id=s.id) pickup_at
           FROM sales s LEFT JOIN players p ON p.id=s.player_id WHERE s.id=?""",
        (sale_id,),
    ).fetchone()
    if not sale or "@" not in (sale["email"] or "").strip():
        return "without_email"
    fully_delivered = not remaining_items
    purchase_time = sale["paid_at"] or sale["created_at"]
    pickup_time = sale["pickup_at"] or sale["delivered_at"] or purchase_time
    item_rows = db.execute(
        """SELECT si.quantity,si.unit_price_cents,p.name,
                  COALESCE((SELECT SUM(sid.quantity) FROM sale_item_deliveries sid
                            WHERE sid.sale_item_id=si.id),0) delivered_quantity
           FROM sale_items si JOIN products p ON p.id=si.product_id
           WHERE si.sale_id=? ORDER BY si.id""",
        (sale_id,),
    ).fetchall()
    # Retiradas antigas podem não ter sido gravadas em sale_item_deliveries;
    # nesse caso o payload recebido pela rota continua sendo a fonte da atualização.
    delivered_by_name = {}
    for item in delivered_items:
        key = str(item["name"])
        delivered_by_name[key] = delivered_by_name.get(key, 0) + int(item["quantity"] or 0)
    rows = []
    for item in item_rows:
        quantity = int(item["quantity"] or 0)
        delivered_total = int(item["delivered_quantity"] or 0)
        delivered_now = delivered_by_name.pop(item["name"], 0)
        if delivered_total == 0 and delivered_now:
            delivered_total = delivered_now
        rows.append({
            "name": item["name"], "quantity": quantity,
            "delivered": delivered_now, "remaining": max(0, quantity - delivered_total),
            "unit_price_cents": int(item["unit_price_cents"] or 0),
        })
    delivered_lines = ", ".join(f"{item['quantity']}x {item['name']}" for item in delivered_items) or "Nenhum item"
    remaining_lines = ", ".join(f"{item['quantity']}x {item['name']}" for item in remaining_items) or "Nenhum item pendente"
    status_text = "Pedido totalmente entregue. Não há itens pendentes." if fully_delivered else "Os itens restantes poderão ser retirados posteriormente."
    lines = [
        f"Olá, {sale['player_name']}!", "", f"Atualização da retirada do pedido #{sale_id} no PELADEIROS GPCTA.", "",
        f"Data e horário da compra: {brdate(purchase_time)}",
        f"Data e horário da retirada: {brdate(pickup_time)}",
        f"CPF: {cpfmask(sale['cpf'])}", f"Forma de pagamento: {sale['payment_method']}", "",
        f"Itens retirados agora: {delivered_lines}.",
        f"Itens restantes: {remaining_lines}.", "",
        status_text, "", f"Total do pedido: {money(sale['total_cents'])}",
    ]
    plain_body = "\n".join(lines)
    esc = html.escape
    status_label = "Pedido totalmente entregue" if fully_delivered else "Retirada parcial registrada"
    status_color = "#198754" if fully_delivered else "#07558c"
    html_rows = "".join(
        f"<tr><td style='padding:10px 8px;border-bottom:1px solid #e5edf2'>{esc(row['name'])}</td>"
        f"<td style='padding:10px 8px;border-bottom:1px solid #e5edf2;text-align:center'>{row['quantity']}</td>"
        f"<td style='padding:10px 8px;border-bottom:1px solid #e5edf2;text-align:center'>{row['delivered']}</td>"
        f"<td style='padding:10px 8px;border-bottom:1px solid #e5edf2;text-align:center'>{row['remaining']}</td>"
        f"<td style='padding:10px 8px;border-bottom:1px solid #e5edf2;text-align:right'>{money(row['unit_price_cents'])}</td>"
        f"<td style='padding:10px 8px;border-bottom:1px solid #e5edf2;text-align:right'>{money(row['quantity'] * row['unit_price_cents'])}</td></tr>"
        for row in rows
    )
    if not html_rows:
        html_rows = "<tr><td colspan='6' style='padding:10px'>Nenhum item encontrado.</td></tr>"
    html_body = f"""<div style='margin:0;background:#f2f6f9;padding:24px;font-family:Arial,sans-serif;color:#183247'>
      <div style='max-width:720px;margin:auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 12px #1232'>
        <div style='background:#07558c;padding:28px 20px;text-align:center'><img src='https://sistema-pelada-one.vercel.app/static/logo-gpcta.jpeg' alt='Logo GPCTA' style='max-width:110px;max-height:90px;object-fit:contain'><h1 style='color:#fff;font-size:26px;margin:12px 0 0'>PELADEIROS GPCTA</h1></div>
        <div style='padding:28px'><h2 style='margin-top:0;color:#07558c'>Atualização da retirada do pedido #{sale_id}</h2>
        <p>Olá, <strong>{esc(sale['player_name'])}</strong>!</p><div style='display:inline-block;background:{status_color};color:#fff;border-radius:6px;padding:8px 12px;font-weight:bold'>{status_label}</div>
        <p style='margin-top:20px'><strong>Pedido:</strong> #{sale_id}<br><strong>CPF:</strong> {esc(cpfmask(sale['cpf']))}<br><strong>Data e horário da compra:</strong> {esc(brdate(purchase_time))}<br><strong>Data e horário da retirada:</strong> {esc(brdate(pickup_time))}<br><strong>Estabelecimento:</strong> PELADEIROS GPCTA<br><strong>Forma de pagamento:</strong> {esc(sale['payment_method'])}</p>
        <h3 style='border-bottom:2px solid #07558c;padding-bottom:8px'>Resumo dos produtos</h3><table style='width:100%;border-collapse:collapse'><thead><tr><th style='text-align:left;padding:8px'>Produto</th><th style='padding:8px'>Qtd.</th><th style='padding:8px'>Retirados</th><th style='padding:8px'>Restantes</th><th style='text-align:right;padding:8px'>Unitário</th><th style='text-align:right;padding:8px'>Total</th></tr></thead><tbody>{html_rows}</tbody></table>
        <p style='font-size:20px;text-align:right'><strong>Total do pedido: {money(sale['total_cents'])}</strong></p><p><strong>{esc(status_text)}</strong></p><p style='color:#607d8b'>Os itens restantes poderão ser retirados posteriormente, quando aplicável.</p></div></div></div>"""
    try:
        send_func(sender, app_password, (sale["email"] or "").strip().lower(), f"Atualização do pedido #{sale_id} - PELADEIROS GPCTA", plain_body, html_body)
    except Exception as exc:
        current_error = str(exc)[:500]
        db.execute("UPDATE sales SET receipt_error=? WHERE id=?", (current_error, sale_id))
        db.commit()
        return "failed"
    return "sent"
