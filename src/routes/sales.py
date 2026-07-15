import uuid
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify, current_app
from itsdangerous import BadData, URLSafeTimedSerializer
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import alphabetical_key, money, datetime_iso, local_today
from src.services.pix import pix_payload, generate_qrcode_base64
from src.services.mercadopago import (
    MercadoPagoError,
    create_pix_order,
    get_order,
    validate_webhook_signature,
)

bp = Blueprint("sales", __name__)
PIX_TOKEN_MAX_AGE = 60 * 60

def pix_access_token(user):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pix-qrcode")
    return serializer.dumps({"user_id": user["id"], "role": user["role"]})

def validate_pix_access_token(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pix-qrcode")
    data = serializer.loads(token, max_age=PIX_TOKEN_MAX_AGE)
    return data.get("role") in ("manager", "staff", "client")

def require_pix_access_token():
    try:
        return validate_pix_access_token(request.headers.get("X-Pix-Token", ""))
    except BadData:
        return False

def mercadopago_config():
    return (
        current_app.config.get("MERCADOPAGO_ACCESS_TOKEN"),
        current_app.config.get("MERCADOPAGO_POS_ID"),
    )

def mercadopago_enabled():
    access_token, _ = mercadopago_config()
    return bool(access_token and current_app.config.get("MERCADOPAGO_WEBHOOK_SECRET"))

def order_payment_id(order):
    payments = (order.get("transactions") or {}).get("payments") or []
    return str(payments[0].get("id")) if payments and payments[0].get("id") else None

def restore_reserved_stock(db, sale_id):
    items = db.execute("SELECT product_id,quantity FROM sale_items WHERE sale_id=?", (sale_id,)).fetchall()
    for item in items:
        db.execute("UPDATE products SET stock=stock+? WHERE id=?", (item["quantity"], item["product_id"]))

def apply_mercadopago_status(db, sale, order):
    status = order.get("status", "")
    detail = order.get("status_detail", "")
    payment_id = order_payment_id(order)
    paid_amount = order.get("total_paid_amount") or order.get("total_amount") or "0"
    try:
        paid_cents = round(float(paid_amount) * 100)
    except (TypeError, ValueError):
        paid_cents = 0

    if status == "processed" and detail == "accredited" and paid_cents == sale["total_cents"]:
        db.execute(
            """UPDATE sales SET paid=1,payment_status='approved',mercadopago_payment_id=?,
               paid_at=CURRENT_TIMESTAMP,ready_for_delivery=1
               WHERE id=? AND paid=0""",
            (payment_id, sale["id"]),
        )
        db.commit()
        return "approved"

    if status == "refunded" and sale["paid"]:
        db.execute(
            "UPDATE sales SET paid=0,payment_status='refunded',mercadopago_payment_id=? WHERE id=?",
            (payment_id, sale["id"]),
        )
        db.commit()
        return "refunded"

    terminal_statuses = {"expired", "canceled"}
    if status in terminal_statuses:
        with db:
            updated = db.execute(
                "UPDATE sales SET paid=0,payment_status=?,mercadopago_payment_id=? WHERE id=? AND paid=0 AND payment_status IN ('creating','pending')",
                (status, payment_id, sale["id"]),
            )
            if updated.rowcount:
                restore_reserved_stock(db, sale["id"])
        return status

    return sale["payment_status"]

@bp.route("/sale", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "client")
def sale():
    db = get_db()
    if request.method == "POST":
        product_ids = request.form.getlist("product_id")
        quantities = request.form.getlist("quantity")
        requested = {}
        try:
            player_id = int(request.form["player_id"])
            for raw_id, raw_qty in zip(product_ids, quantities):
                qty = int(raw_qty or 0)
                if qty > 0:
                    requested[int(raw_id)] = requested.get(int(raw_id), 0) + qty
            if not requested:
                raise ValueError("Escolha ao menos um produto.")
            
            placeholders = ",".join("?" for _ in requested)
            products_by_id = {
                r["id"]: r for r in db.execute(
                    f"SELECT * FROM products WHERE active=1 AND id IN ({placeholders})",
                    tuple(requested)
                )
            }
            if len(products_by_id) != len(requested):
                raise ValueError("Produto inválido ou inativo.")
            
            for pid, qty in requested.items():
                if products_by_id[pid]["stock"] < qty:
                    raise ValueError(f"Estoque insuficiente de {products_by_id[pid]['name']}.")
            
            total = sum(products_by_id[pid]["price_cents"] * qty for pid, qty in requested.items())
            method = request.form["payment_method"]
            if method == "Pix" and mercadopago_enabled():
                raise ValueError("Para pagamentos Pix, gere o QR Code e aguarde a confirmação automática.")
            if g.user["role"] == "client" and method not in ("Pix", "Dinheiro"):
                raise ValueError("Clientes podem registrar pagamentos somente em Pix ou Dinheiro.")
            
            cash_pending = method == "Dinheiro"
            paid = 0 if cash_pending else 1
            payment_status = "pending_cash" if cash_pending else "approved"
            with db:
                cur = db.execute(
                    """INSERT INTO sales
                       (player_id,payment_method,total_cents,paid,payment_status,paid_at,ready_for_delivery,notes)
                       VALUES(?,?,?,?,?,CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE NULL END,1,?)""",
                    (player_id, method, total, paid, payment_status, paid,
                     request.form.get("notes", "").strip())
                )
                for pid, qty in requested.items():
                    product = products_by_id[pid]
                    db.execute(
                        "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                        (cur.lastrowid, pid, qty, product["price_cents"], product["cost_cents"])
                    )
                    updated = db.execute(
                        "UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                        (qty, pid, qty)
                    )
                    if updated.rowcount != 1:
                        raise ValueError("O estoque mudou durante a venda. Tente novamente.")
            
            if cash_pending:
                flash(
                    f"Pedido #{cur.lastrowid} enviado. Pague {money(total)} em dinheiro para a atendente retirar os produtos.",
                    "success",
                )
            else:
                flash(f"Venda #{cur.lastrowid} registrada: {money(total)}.", "success")
            return redirect(url_for("sales.sale"), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao processar venda: {exc}")
            flash("Erro interno ao processar a venda. Tente novamente.", "danger")

    player_rows = db.execute("SELECT * FROM players WHERE active=1").fetchall()
    player_rows = sorted(
        player_rows,
        key=lambda player: alphabetical_key(player["war_name"] or player["name"]),
    )
    product_rows = db.execute("SELECT * FROM products WHERE active=1 AND stock>0 ORDER BY category, name").fetchall()
    return render_template(
        "sale.html",
        players=player_rows,
        products=product_rows,
        pix_token=pix_access_token(g.user),
        mercadopago_enabled=mercadopago_enabled(),
    )

@bp.post("/sales/<int:sale_id>/delete")
@roles_allowed("manager", "staff")
def delete_sale(sale_id):
    db = get_db()
    sale_row = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale_row:
        flash("Venda não encontrada ou já apagada.", "warning")
        return redirect(request.referrer or url_for("finance.reports"))
    
    try:
        items = db.execute(
            "SELECT product_id, quantity FROM sale_items WHERE sale_id=?", (sale_id,)
        ).fetchall()
        with db:
            if sale_row["payment_status"] not in ("failed", "expired", "canceled"):
                for item in items:
                    db.execute(
                        "UPDATE products SET stock=stock+? WHERE id=?",
                        (item["quantity"], item["product_id"]),
                    )
            db.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        flash(f"Venda #{sale_id} apagada e itens devolvidos ao estoque.", "success")
    except Exception as exc:
        current_app.logger.error(f"Erro ao deletar venda {sale_id}: {exc}")
        flash("Erro interno ao apagar a venda.", "danger")
    return redirect(request.referrer or url_for("finance.reports"))

@bp.route("/pix")
@roles_allowed("manager", "staff")
def pix():
    db = get_db()
    day = request.args.get("day", local_today().isoformat())
    try:
        date.fromisoformat(day)
    except ValueError:
        day = local_today().isoformat()
        flash("A data informada era inválida; exibimos os pagamentos de hoje.", "warning")
    rows = db.execute(
        """SELECT s.*,p.name player_name,COALESCE(s.paid_at,s.created_at) payment_time
        FROM sales s JOIN players p ON p.id=s.player_id
        WHERE date(COALESCE(s.paid_at,s.created_at))=?
          AND s.payment_method='Pix' AND s.paid=1
        ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC""",
        (day,)
    ).fetchall()
    total = sum(r["total_cents"] for r in rows)
    return render_template("pix.html", rows=rows, total=total, day=day)

@bp.get("/orders")
@roles_allowed("manager", "staff")
def orders():
    return render_template("orders.html")

def delivery_order_data(db, sale):
    items = db.execute(
        """SELECT si.quantity,p.name FROM sale_items si
           JOIN products p ON p.id=si.product_id WHERE si.sale_id=? ORDER BY si.id""",
        (sale["id"],),
    ).fetchall()
    return {
        "id": sale["id"],
        "player_name": sale["war_name"] or sale["player_name"],
        "total_cents": sale["total_cents"],
        "payment_method": sale["payment_method"],
        "payment_status": sale["payment_status"],
        "paid": bool(sale["paid"]),
        "waiting_cash": sale["payment_status"] == "pending_cash" and not sale["paid"],
        "notes": sale["notes"] or "",
        "paid_at": datetime_iso(sale["paid_at"] or sale["created_at"]),
        "delivered_at": datetime_iso(sale["delivered_at"]),
        "delivered_by_name": sale["delivered_by_name"] or "",
        "items": [{"name": item["name"], "quantity": item["quantity"]} for item in items],
    }

@bp.get("/orders/feed")
@roles_allowed("manager", "staff")
def orders_feed():
    db = get_db()
    select = """SELECT s.*,p.name player_name,p.war_name,u.name delivered_by_name
                FROM sales s JOIN players p ON p.id=s.player_id
                LEFT JOIN users u ON u.id=s.delivered_by"""
    pending = db.execute(
        f"""{select} WHERE s.ready_for_delivery=1 AND s.delivered_at IS NULL
             AND (s.paid=1 OR s.payment_status='pending_cash')
             ORDER BY COALESCE(s.paid_at,s.created_at),s.id"""
    ).fetchall()
    delivered = db.execute(
        f"{select} WHERE s.ready_for_delivery=1 AND s.delivered_at IS NOT NULL ORDER BY s.delivered_at DESC LIMIT 20"
    ).fetchall()
    return jsonify(
        pending=[delivery_order_data(db, sale) for sale in pending],
        delivered=[delivery_order_data(db, sale) for sale in delivered],
    )

@bp.post("/orders/<int:sale_id>/deliver")
@roles_allowed("manager", "staff")
def deliver_order(sale_id):
    db = get_db()
    updated = db.execute(
        """UPDATE sales SET paid=1,payment_status='approved',
           paid_at=COALESCE(paid_at,CURRENT_TIMESTAMP),
           delivered_at=CURRENT_TIMESTAMP,delivered_by=?
           WHERE id=? AND ready_for_delivery=1 AND delivered_at IS NULL
           AND (paid=1 OR payment_status='pending_cash')""",
        (g.user["id"], sale_id),
    )
    db.commit()
    if updated.rowcount != 1:
        return jsonify(error="Pedido não encontrado ou já entregue."), 409
    return jsonify(ok=True, sale_id=sale_id)

@bp.post("/orders/<int:sale_id>/cancel")
@roles_allowed("manager", "staff")
def cancel_cash_order(sale_id):
    db = get_db()
    try:
        with db:
            updated = db.execute(
                """UPDATE sales SET payment_status='canceled',ready_for_delivery=0
                   WHERE id=? AND payment_method='Dinheiro' AND paid=0
                   AND payment_status='pending_cash' AND delivered_at IS NULL""",
                (sale_id,),
            )
            if updated.rowcount != 1:
                return jsonify(error="Pedido em dinheiro não encontrado ou já finalizado."), 409
            restore_reserved_stock(db, sale_id)
    except Exception as exc:
        current_app.logger.error(f"Erro ao cancelar pedido em dinheiro {sale_id}: {exc}")
        return jsonify(error="Não foi possível cancelar o pedido."), 500
    return jsonify(ok=True, sale_id=sale_id)

@bp.get("/pix/qrcode")
def pix_qrcode():
    try:
        if not validate_pix_access_token(request.headers.get("X-Pix-Token", "")):
            raise BadData
    except BadData:
        return jsonify(error="A autorização do Pix expirou. Recarregue a página e tente novamente."), 401

    try:
        amount_cents = int(request.args.get("amount_cents", 0))
        if amount_cents <= 0 or amount_cents > 100_000_000:
            raise ValueError
    except ValueError:
        return jsonify(error="Selecione produtos para gerar um Pix com valor válido."), 400
    
    try:
        payload = pix_payload(
            amount_cents,
            current_app.config["PIX_KEY"],
            current_app.config["PIX_MERCHANT_NAME"],
            current_app.config["PIX_MERCHANT_CITY"]
        )
        encoded_image = generate_qrcode_base64(payload)
        return jsonify(
            payload=payload,
            image=f"data:image/png;base64,{encoded_image}",
            key=current_app.config["PIX_KEY"],
            amount=money(amount_cents),
        )
    except Exception as exc:
        current_app.logger.error(f"Erro ao gerar QR Code de Pix: {exc}")
        return jsonify(error="Erro interno ao gerar o QR Code de Pix."), 500

@bp.post("/pix/mercadopago/orders")
def mercadopago_create_order():
    if not require_pix_access_token():
        return jsonify(error="A autorização do Pix expirou. Recarregue a página e tente novamente."), 401
    access_token, _ = mercadopago_config()
    if not access_token:
        return jsonify(error="A integração com Mercado Pago ainda não foi configurada."), 503

    body = request.get_json(silent=True) or {}
    try:
        player_id = int(body.get("player_id"))
        requested = {}
        for item in body.get("items") or []:
            product_id = int(item.get("product_id"))
            quantity = int(item.get("quantity"))
            if quantity > 0:
                requested[product_id] = requested.get(product_id, 0) + quantity
        if not requested:
            raise ValueError("Escolha ao menos um produto.")
    except (TypeError, ValueError):
        return jsonify(error="Selecione o peladeiro e produtos válidos."), 400

    db = get_db()
    player = db.execute("SELECT id,email FROM players WHERE id=? AND active=1", (player_id,)).fetchone()
    placeholders = ",".join("?" for _ in requested)
    products = db.execute(
        f"SELECT * FROM products WHERE active=1 AND id IN ({placeholders})", tuple(requested)
    ).fetchall()
    products_by_id = {product["id"]: product for product in products}
    if not player or len(products_by_id) != len(requested):
        return jsonify(error="Peladeiro ou produto inválido."), 400
    payer_email = str(player["email"] or "").strip().lower()
    if "@" not in payer_email:
        return jsonify(error="Cadastre um e-mail válido para o peladeiro antes de gerar o Pix."), 400
    for product_id, quantity in requested.items():
        if products_by_id[product_id]["stock"] < quantity:
            return jsonify(error=f"Estoque insuficiente de {products_by_id[product_id]['name']}."), 409

    total_cents = sum(products_by_id[product_id]["price_cents"] * quantity for product_id, quantity in requested.items())
    external_reference = f"pelada_{uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    try:
        with db:
            sale_cursor = db.execute(
                """INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,external_reference,idempotency_key,notes)
                   VALUES(?,?,?,'0','creating',?,?,?)""",
                (player_id, "Pix", total_cents, external_reference, idempotency_key, str(body.get("notes") or "").strip()),
            )
            sale_id = sale_cursor.lastrowid
            for product_id, quantity in requested.items():
                product = products_by_id[product_id]
                db.execute(
                    "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                    (sale_id, product_id, quantity, product["price_cents"], product["cost_cents"]),
                )
                updated = db.execute(
                    "UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                    (quantity, product_id, quantity),
                )
                if updated.rowcount != 1:
                    raise ValueError(f"O estoque de {product['name']} mudou. Tente novamente.")
    except ValueError as exc:
        return jsonify(error=str(exc)), 409

    try:
        order = create_pix_order(access_token, external_reference, total_cents, idempotency_key, payer_email)
        order_id = order.get("id")
        payments = (order.get("transactions") or {}).get("payments") or []
        payment_method = (payments[0].get("payment_method") or {}) if payments else {}
        qr_data = payment_method.get("qr_code")
        if not order_id or not qr_data:
            raise MercadoPagoError("O Mercado Pago não retornou o QR Code Pix.")
        db.execute(
            """UPDATE sales SET mercadopago_order_id=?,mercadopago_payment_id=?,
               payment_status=CASE WHEN payment_status='creating' THEN 'pending' ELSE payment_status END WHERE id=?""",
            (order_id, order_payment_id(order), sale_id),
        )
        db.commit()
        encoded_image = generate_qrcode_base64(qr_data)
        return jsonify(
            sale_id=sale_id,
            order_id=order_id,
            payload=qr_data,
            image=f"data:image/png;base64,{encoded_image}",
            amount=money(total_cents),
            status="pending",
            status_url=url_for("sales.mercadopago_order_status", sale_id=sale_id),
        ), 201
    except Exception as exc:
        current_app.logger.error(f"Erro ao criar order Mercado Pago: {exc}")
        with db:
            sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            if sale and sale["payment_status"] == "creating":
                restore_reserved_stock(db, sale_id)
                db.execute("UPDATE sales SET payment_status='failed' WHERE id=?", (sale_id,))
        message = str(exc) if isinstance(exc, MercadoPagoError) else "Não foi possível criar a cobrança no Mercado Pago."
        return jsonify(error=message), 502

@bp.get("/pix/mercadopago/orders/<int:sale_id>/status")
def mercadopago_order_status(sale_id):
    if not require_pix_access_token():
        return jsonify(error="A autorização do Pix expirou. Recarregue a página."), 401
    access_token, _ = mercadopago_config()
    db = get_db()
    sale = db.execute("SELECT * FROM sales WHERE id=? AND payment_method='Pix'", (sale_id,)).fetchone()
    if not sale:
        return jsonify(error="Cobrança não encontrada."), 404
    if sale["payment_status"] == "pending" and sale["mercadopago_order_id"] and access_token:
        try:
            order = get_order(access_token, sale["mercadopago_order_id"])
            apply_mercadopago_status(db, sale, order)
            sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        except MercadoPagoError as exc:
            current_app.logger.warning(f"Falha ao consultar order {sale['mercadopago_order_id']}: {exc}")
    return jsonify(status=sale["payment_status"], paid=bool(sale["paid"]), sale_id=sale_id)

@bp.post("/webhooks/mercadopago")
def mercadopago_webhook():
    payload = request.get_json(silent=True) or {}
    notification_data = payload.get("data") or {}
    data_id = request.args.get("data.id") or notification_data.get("id")
    secret = current_app.config.get("MERCADOPAGO_WEBHOOK_SECRET")
    if not validate_webhook_signature(
        request.headers.get("X-Signature", ""),
        request.headers.get("X-Request-Id", ""),
        str(data_id or ""),
        secret,
    ):
        return "", 401

    # A aplicação processa somente orders do Pix online. O simulador do painel
    # envia uma order genérica do Point (`type=point`) com ID fictício; depois
    # de validar a assinatura, basta confirmar o recebimento desse evento.
    if notification_data.get("type") not in (None, "online"):
        return "", 200

    try:
        db = get_db()
        sale = db.execute(
            "SELECT * FROM sales WHERE mercadopago_order_id=? OR external_reference=?",
            (str(data_id or ""), notification_data.get("external_reference")),
        ).fetchone()
        if not sale:
            # O simulador usa IDs fictícios. Uma notificação válida, mas sem uma
            # cobrança local correspondente, deve apenas ser reconhecida.
            return "", 200

        order = notification_data
        if not order.get("status"):
            access_token, _ = mercadopago_config()
            if not access_token:
                return "", 503
            order = get_order(access_token, str(data_id))
        apply_mercadopago_status(db, sale, order)
        return "", 200
    except Exception as exc:
        current_app.logger.error(f"Erro ao processar webhook Mercado Pago: {exc}")
        return "", 500
