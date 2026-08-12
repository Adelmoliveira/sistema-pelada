import uuid
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify, current_app, send_file
from itsdangerous import BadData, URLSafeTimedSerializer
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import alphabetical_key, money, datetime_iso, brdate, local_today
from src.services.pix import pix_payload, generate_qrcode_base64
from src.services.mercadopago import (
    MercadoPagoError,
    create_pix_order,
    get_order,
    validate_webhook_signature,
)
from src.services.stock_alerts import notify_low_stock
from src.services.purchase_receipts import send_purchase_receipt, send_delivery_update
from src.services.push_notifications import send_player_push_once
from src.services.bar_credits import approve_topup, balance as credit_balance, consume as consume_credit, low_balance_threshold, notify_low_balance
from src.services.pending_delivery_pdf import build_pending_delivery_pdf

bp = Blueprint("sales", __name__)
PIX_TOKEN_MAX_AGE = 60 * 60

def pix_access_token(user):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pix-qrcode")
    return serializer.dumps({"user_id": user["id"], "role": user["role"]})

def event_public_token(event_id):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="event-sale")
    return serializer.dumps({"event_id": int(event_id), "role": "event_guest"})

def validate_event_public_token(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="event-sale")
    data = serializer.loads(token, max_age=60 * 60 * 24 * 365)
    if data.get("role") != "event_guest":
        raise BadData("token inválido")
    return int(data["event_id"])

def validate_pix_access_token(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pix-qrcode")
    data = serializer.loads(token, max_age=PIX_TOKEN_MAX_AGE)
    return data.get("role") in ("manager", "staff", "client", "event_guest")

def require_pix_access_token():
    # A guest event sale uses the event token in the same header used by the
    # regular Pix flow.  Try both token formats without allowing an invalid
    # token to raise a 500 response.
    pix_token = request.headers.get("X-Pix-Token", "")
    try:
        if validate_pix_access_token(pix_token):
            return True
    except BadData:
        pass
    for token in (request.headers.get("X-Event-Token", ""), pix_token):
        try:
            if validate_event_public_token(token):
                return True
        except (BadData, ValueError, TypeError):
            pass
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
            sale_type = request.form.get("sale_type", "player").strip().lower()
            event_id = None
            guest_name = ""
            if g.user["role"] == "client":
                sale_type = "player"
                player_id = int(g.user["player_id"] or request.form.get("player_id") or 0)
                if not player_id:
                    raise ValueError("Seu usuário ainda não está vinculado a um peladeiro.")
                if g.user["player_id"] and request.form.get("player_id") and int(request.form["player_id"]) != int(g.user["player_id"]):
                    raise ValueError("O pedido só pode ser registrado para o peladeiro conectado.")
            elif sale_type == "event":
                player_id = None
                try:
                    event_id = int(request.form.get("event_id") or 0)
                except (TypeError, ValueError):
                    event_id = 0
                guest_name = request.form.get("guest_name", "").strip()
                if not event_id or not guest_name:
                    raise ValueError("Selecione o evento e informe o nome do convidado.")
                event = db.execute("SELECT id FROM bar_events WHERE id=? AND status='open'", (event_id,)).fetchone()
                if not event:
                    raise ValueError("Este evento não está aberto para vendas.")
            else:
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
                    f"SELECT id,name,price_cents,cost_cents,stock FROM products WHERE active=1 AND id IN ({placeholders})",
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
            if sale_type == "event" and method not in ("Pix", "Dinheiro", "Débito", "Cortesia"):
                raise ValueError("Vendas de evento não podem usar créditos de peladeiro.")
            if g.user["role"] == "client" and method not in ("Pix", "Dinheiro", "Créditos"):
                raise ValueError("Clientes podem registrar pagamentos somente em Pix ou Dinheiro.")
            
            cash_pending = method == "Dinheiro"
            paid = 0 if cash_pending else 1
            payment_status = "pending_cash" if cash_pending else "approved"
            low_credit_balance = None
            with db:
                cur = db.execute(
                    """INSERT INTO sales
                       (player_id,event_id,guest_name,payment_method,total_cents,paid,payment_status,paid_at,ready_for_delivery,notes)
                       VALUES(?,?,?,?,?,?,?,CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE NULL END,1,?)""",
                    (player_id, event_id, guest_name, method, total, paid, payment_status, paid,
                     request.form.get("notes", "").strip())
                )
                if method == "Créditos":
                    if g.user["role"] != "client":
                        raise ValueError("Somente o peladeiro pode pagar com créditos.")
                    paid = 1
                    payment_status = "approved"
                    db.execute("UPDATE sales SET paid=1,payment_status='approved',paid_at=CURRENT_TIMESTAMP WHERE id=?", (cur.lastrowid,))
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
                if method == "Créditos":
                    low_credit_balance, should_notify = consume_credit(db, player_id, total, cur.lastrowid, g.user["id"])
            notify_low_stock(db, requested.keys())
            if method == "Créditos" and low_credit_balance is not None:
                notify_low_balance(db, player_id, low_credit_balance)
            
            flash(f"Pedido registrado com sucesso! Pedido #{cur.lastrowid}.", "success")
            return redirect(url_for("sales.sale", cart_cleared=1), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao processar venda: {exc}")
            flash("Erro interno ao processar a venda. Tente novamente.", "danger")

    player_select = """SELECT p.id,p.name,p.war_name,p.thumbnail_data,
                         COALESCE((SELECT SUM(s.total_cents) FROM sales s
                         WHERE s.player_id=p.id AND s.paid=1), 0) accumulated_total_cents
                      FROM players p WHERE p.active=1"""
    if g.user["role"] == "client":
        player_rows = db.execute(f"{player_select} AND p.id=?", (g.user["player_id"],)).fetchall()
    else:
        player_rows = db.execute(player_select).fetchall()
    player_rows = sorted(
        player_rows,
        key=lambda player: alphabetical_key(player["war_name"] or player["name"]),
    )
    product_rows = db.execute(
        """SELECT p.id,p.name,p.category,p.package_type,p.units_per_case,p.price_cents,
                  p.cost_cents,p.stock,p.min_stock,p.thumbnail_data,
                  COALESCE(SUM(CASE WHEN s.paid=1 THEN si.quantity ELSE 0 END), 0) sold_quantity
           FROM products p
           LEFT JOIN sale_items si ON si.product_id=p.id
           LEFT JOIN sales s ON s.id=si.sale_id
           WHERE p.active=1 AND p.stock>0
           GROUP BY p.id"""
    ).fetchall()
    product_data = []
    beverage_categories = {"cerveja", "refrigerante", "água mineral com gás", "água mineral sem gás", "energético", "suco", "isotônico"}
    snack_categories = {"salgadinho", "salgados", "salgado"}
    for row in product_rows:
        product = dict(row)
        # A venda rápida usa apenas a miniatura. Evita enviar a foto completa
        # em base64 dentro do JSON da página para manter o PWA leve no celular.
        product.pop("photo_data", None)
        category = (product.get("category") or "").strip().lower()
        product["group"] = "Bebidas" if category in beverage_categories or "bebida" in category else "Salgados" if category in snack_categories or "salgad" in category else "Alimentos" if "alimento" in category else "Outros"
        product_data.append(product)
    product_data.sort(key=lambda product: (-int(product.get("sold_quantity") or 0), (product.get("category") or "").lower(), (product.get("name") or "").lower()))
    product_rows = product_data
    client_credit_balance = credit_balance(db, g.user["player_id"])["balance_cents"] if g.user["role"] == "client" and g.user["player_id"] else 0
    open_events = db.execute(
        "SELECT id,name,event_date FROM bar_events WHERE status='open' ORDER BY event_date DESC,id DESC"
    ).fetchall() if g.user["role"] in ("manager", "staff") else []
    product_groups = [group for group in ("Bebidas", "Alimentos", "Salgados", "Outros") if any(product["group"] == group for product in product_data)]
    return render_template(
        "sale.html",
        players=player_rows,
        products=product_rows,
        product_data=product_data,
        product_groups=product_groups,
        player_data=[{
            "id": player["id"],
            "full_name": player["name"],
            "war_name": player["war_name"] or "",
            "photo": player["thumbnail_data"] or "",
            "accumulated_total_cents": int(player["accumulated_total_cents"] or 0),
        } for player in player_rows],
        pix_token=pix_access_token(g.user),
        mercadopago_enabled=mercadopago_enabled(),
        client_credit_balance=int(client_credit_balance or 0),
        client_credit_low_threshold=low_balance_threshold(),
        open_events=open_events,
    )

@bp.route("/evento/<token>/venda", methods=["GET", "POST"])
def guest_event_sale(token):
    """Venda pública iniciada pelo QR Code de um evento aberto."""
    try:
        event_id = validate_event_public_token(token)
    except (BadData, ValueError, TypeError):
        return "Link do evento inválido ou expirado.", 404
    db = get_db()
    event = db.execute("SELECT * FROM bar_events WHERE id=? AND status='open'", (event_id,)).fetchone()
    if not event:
        return "Este evento já foi encerrado.", 410
    if request.method == "POST":
        requested = {}
        try:
            guest_name = request.form.get("guest_name", "").strip()
            method = request.form.get("payment_method", "").strip()
            if not guest_name:
                raise ValueError("Informe seu nome para registrar o pedido.")
            if method == "Pix" and mercadopago_enabled():
                raise ValueError("Para pagar via Pix, gere o QR Code e aguarde a confirmação.")
            if method not in ("Pix", "Dinheiro", "Débito", "Cortesia"):
                raise ValueError("Escolha uma forma de pagamento válida.")
            for raw_id, raw_qty in zip(request.form.getlist("product_id"), request.form.getlist("quantity")):
                qty = int(raw_qty or 0)
                if qty > 0:
                    requested[int(raw_id)] = requested.get(int(raw_id), 0) + qty
            if not requested:
                raise ValueError("Escolha ao menos um produto.")
            placeholders = ",".join("?" for _ in requested)
            products = db.execute(
                f"SELECT id,name,price_cents,cost_cents,stock FROM products WHERE active=1 AND id IN ({placeholders})",
                tuple(requested),
            ).fetchall()
            products_by_id = {row["id"]: row for row in products}
            if len(products_by_id) != len(requested):
                raise ValueError("Produto inválido ou inativo.")
            for product_id, quantity in requested.items():
                if products_by_id[product_id]["stock"] < quantity:
                    raise ValueError(f"Estoque insuficiente de {products_by_id[product_id]['name']}.")
            total_cents = sum(products_by_id[pid]["price_cents"] * qty for pid, qty in requested.items())
            with db:
                paid = 0 if method == "Dinheiro" else 1
                payment_status = "pending_cash" if method == "Dinheiro" else "approved"
                cur = db.execute(
                    """INSERT INTO sales(player_id,event_id,guest_name,payment_method,total_cents,paid,payment_status,ready_for_delivery,notes)
                       VALUES(NULL,?,?,?,?,?,?,1,?)""",
                    (event_id, guest_name, method, total_cents, paid, payment_status, request.form.get("notes", "").strip()),
                )
                for product_id, quantity in requested.items():
                    product = products_by_id[product_id]
                    db.execute(
                        "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                        (cur.lastrowid, product_id, quantity, product["price_cents"], product["cost_cents"]),
                    )
                    updated = db.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?", (quantity, product_id, quantity))
                    if updated.rowcount != 1:
                        raise ValueError("O estoque mudou durante a venda. Tente novamente.")
            notify_low_stock(db, requested.keys())
            flash(f"Pedido registrado com sucesso! Pedido #{cur.lastrowid}.", "success")
            return redirect(url_for("sales.guest_event_sale", token=token, cart_cleared=1), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.exception("Erro ao registrar venda pública do evento %s: %s", event_id, exc)
            flash("Erro interno ao processar a venda. Tente novamente.", "danger")

    product_rows = db.execute(
        """SELECT p.id,p.name,p.category,p.package_type,p.units_per_case,p.price_cents,
                  p.cost_cents,p.stock,p.min_stock,p.thumbnail_data,
                  COALESCE(SUM(CASE WHEN s.paid=1 THEN si.quantity ELSE 0 END),0) sold_quantity
           FROM products p LEFT JOIN sale_items si ON si.product_id=p.id LEFT JOIN sales s ON s.id=si.sale_id
           WHERE p.active=1 AND p.stock>0 GROUP BY p.id"""
    ).fetchall()
    product_data = []
    beverage_categories = {"cerveja", "refrigerante", "água mineral com gás", "água mineral sem gás", "energético", "suco", "isotônico"}
    snack_categories = {"salgadinho", "salgados", "salgado"}
    for row in product_rows:
        product = dict(row)
        product.pop("photo_data", None)
        category = (product.get("category") or "").strip().lower()
        product["group"] = "Bebidas" if category in beverage_categories or "bebida" in category else "Salgados" if category in snack_categories or "salgad" in category else "Alimentos" if "alimento" in category else "Outros"
        product_data.append(product)
    product_data.sort(key=lambda product: (-int(product.get("sold_quantity") or 0), (product.get("category") or "").lower(), (product.get("name") or "").lower()))
    guest_user = {"id": 0, "name": "Convidado", "role": "guest"}
    return render_template(
        "sale.html", guest_mode=True, guest_event=event, guest_event_token=token,
        current_user=guest_user, current_player=None, players=[], player_data=[], products=product_data,
        product_data=product_data, product_groups=[group for group in ("Bebidas", "Alimentos", "Salgados", "Outros") if any(p["group"] == group for p in product_data)],
        pix_token=token, event_pix_token=token, mercadopago_enabled=mercadopago_enabled(),
        client_credit_balance=0, client_credit_low_threshold=0, open_events=[]
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
        notify_low_stock(db, [item["product_id"] for item in items])
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
        """SELECT s.*,COALESCE(p.name,s.guest_name,'Convidado') player_name,e.name event_name,
        COALESCE(s.paid_at,s.created_at) payment_time
        FROM sales s LEFT JOIN players p ON p.id=s.player_id LEFT JOIN bar_events e ON e.id=s.event_id
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

@bp.get("/orders/delivered")
@roles_allowed("manager", "staff")
def delivered_history():
    return render_template("order_history.html", history_kind="delivered")

@bp.get("/orders/canceled")
@roles_allowed("manager", "staff")
def canceled_history():
    return render_template("order_history.html", history_kind="canceled")


def pending_delivery_orders(db):
    """Return paid orders with at least one item still awaiting pickup.

    This is deliberately independent from the operational delivery feed: the
    page using it has no delivery actions and is intended only for counting
    and planning the pending withdrawals.
    """
    select = """SELECT s.*,p.name player_name,p.war_name,p.thumbnail_data player_thumbnail_data,
                e.name event_name,u.name delivered_by_name
                FROM sales s LEFT JOIN players p ON p.id=s.player_id
                LEFT JOIN bar_events e ON e.id=s.event_id
                LEFT JOIN users u ON u.id=s.delivered_by"""
    sales = db.execute(
        f"""{select}
             WHERE s.paid=1 AND s.delivered_at IS NULL
               AND (s.ready_for_delivery=1 OR s.event_id IS NOT NULL)
             ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC"""
    ).fetchall()
    result = []
    for sale in sales:
        order = delivery_order_data(db, sale)
        if order["pending_quantity"] > 0:
            result.append(order)
    return result


@bp.get("/orders/pending-delivery")
@roles_allowed("manager", "staff")
def pending_delivery():
    orders = pending_delivery_orders(get_db())
    return render_template(
        "pending_delivery.html",
        orders=orders,
        total_orders=len(orders),
        total_items=sum(order["pending_quantity"] for order in orders),
    )


@bp.get("/orders/pending-delivery.pdf")
@roles_allowed("manager", "staff")
def pending_delivery_pdf():
    orders = pending_delivery_orders(get_db())
    pdf = build_pending_delivery_pdf(orders)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="pedidos-aguardando-retirada.pdf",
    )

def delivery_order_data(db, sale):
    # Normalize rows before reading optional columns.  sqlite3.Row and the
    # PostgreSQL adapters do not expose ``keys`` in exactly the same way; in
    # particular, iterating over ``row.keys`` (without calling it) raises
    # ``'builtin_function_or_method' object is not iterable`` on the recent
    # deliveries page.
    sale_keys = getattr(sale, "keys", None)
    if callable(sale_keys):
        sale_keys = sale_keys()
    sale_keys = set(sale_keys or ())
    sale = {key: sale[key] for key in sale_keys}
    items = db.execute(
        """SELECT si.id,si.quantity,p.name,
                  COALESCE((SELECT SUM(sid.quantity) FROM sale_item_deliveries sid WHERE sid.sale_item_id=si.id),0) delivered_quantity
           FROM sale_items si
           JOIN products p ON p.id=si.product_id WHERE si.sale_id=? ORDER BY si.id""",
        (sale["id"],),
    ).fetchall()
    # Compatibilidade com pedidos antigos: antes da retirada parcial, os
    # detalhes em sale_item_deliveries não eram gravados. Um pedido com
    # delivered_at preenchido, mas sem nenhum detalhe, foi integralmente
    # entregue e não deve exibir itens pendentes.
    item_data_rows = []
    for item in items:
        item_keys = getattr(item, "keys", None)
        if callable(item_keys):
            item_keys = item_keys()
        item_data_rows.append({key: item[key] for key in set(item_keys or ())})
    items = item_data_rows
    if sale.get("delivered_at") and items and not any(int(item.get("delivered_quantity") or 0) > 0 for item in items):
        items = [dict(item, delivered_quantity=item.get("quantity")) for item in items]
    item_data = [{
        "id": item["id"], "name": item["name"], "quantity": int(item["quantity"] or 0),
        "delivered_quantity": int(item["delivered_quantity"] or 0),
        "pending_quantity": max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0)),
    } for item in items]
    delivered_quantity = sum(item["delivered_quantity"] for item in item_data)
    pending_quantity = sum(item["pending_quantity"] for item in item_data)
    return {
        "id": sale["id"],
        "player_name": sale.get("guest_name") or sale.get("war_name") or sale.get("player_name") or f"Convidado #{sale['id']}",
        "player_full_name": sale.get("player_name") or sale.get("guest_name") or "",
        "player_war_name": sale.get("war_name") or "",
        "player_photo": sale.get("player_thumbnail_data") or "",
        "event_name": sale.get("event_name") or "",
        "is_event": bool(sale.get("event_id")),
        "guest_name": sale.get("guest_name") or "",
        "total_cents": sale.get("total_cents", 0),
        "payment_method": sale.get("payment_method") or "",
        "payment_status": sale.get("payment_status") or "",
        "paid": bool(sale.get("paid")),
        "waiting_cash": sale.get("payment_status") == "pending_cash" and not sale.get("paid"),
        "notes": sale.get("notes") or "",
        "paid_at": datetime_iso(sale.get("paid_at") or sale.get("created_at")),
        "delivered_at": datetime_iso(sale.get("delivered_at")),
        "delivered_by_name": sale.get("delivered_by_name") or "",
        "canceled": sale.get("payment_status") == "canceled",
        "canceled_at": datetime_iso(sale.get("canceled_at")) if "canceled_at" in sale else None,
        "canceled_by_name": sale.get("canceled_by_name") or "",
        "cancellation_reason": sale.get("cancellation_reason") or "",
        "partial": delivered_quantity > 0 and pending_quantity > 0,
        "delivered_quantity": delivered_quantity,
        "pending_quantity": pending_quantity,
        "receipt_url": url_for("sales.receipt", sale_id=sale["id"]),
        "items": item_data,
    }

@bp.get("/orders/feed")
@roles_allowed("manager", "staff")
def orders_feed():
    db = get_db()
    payment_method = request.args.get("payment_method", "").strip()
    if payment_method not in {"", "Pix", "Dinheiro", "Débito", "Cortesia", "Créditos"}:
        payment_method = ""
    payment_clause = " AND s.payment_method=?" if payment_method else ""
    payment_params = (payment_method,) if payment_method else ()
    select = """SELECT s.*,p.name player_name,p.war_name,p.thumbnail_data player_thumbnail_data,e.name event_name,
                u.name delivered_by_name,cu.name canceled_by_name,sc.reason cancellation_reason,sc.canceled_at
                FROM sales s LEFT JOIN players p ON p.id=s.player_id LEFT JOIN bar_events e ON e.id=s.event_id
                LEFT JOIN users u ON u.id=s.delivered_by
                LEFT JOIN sale_cancellations sc ON sc.sale_id=s.id
                LEFT JOIN users cu ON cu.id=sc.canceled_by"""
    pending = db.execute(
        f"""{select} WHERE (s.ready_for_delivery=1 OR (s.event_id IS NOT NULL AND s.delivered_at IS NULL))
             AND s.delivered_at IS NULL
             AND (s.paid=1 OR s.payment_status='pending_cash'){payment_clause}
             ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC""", payment_params
    ).fetchall()
    delivered = db.execute(
        f"{select} WHERE s.ready_for_delivery=1 AND s.delivered_at IS NOT NULL{payment_clause} ORDER BY s.delivered_at DESC LIMIT 20",
        payment_params,
    ).fetchall()
    canceled = db.execute(
        f"{select} WHERE s.payment_status='canceled' AND sc.canceled_at IS NOT NULL{payment_clause} ORDER BY sc.canceled_at DESC LIMIT 20",
        payment_params,
    ).fetchall()
    return jsonify(
        pending=[delivery_order_data(db, sale) for sale in pending],
        delivered=[delivery_order_data(db, sale) for sale in delivered],
        canceled=[delivery_order_data(db, sale) for sale in canceled],
        payment_method=payment_method,
    )

@bp.post("/orders/<int:sale_id>/deliver")
@roles_allowed("manager", "staff")
def deliver_order(sale_id):
    db = get_db()
    remaining_items = []
    payload = request.get_json(silent=True) or {}
    requested_item_id = payload.get("sale_item_id")
    requested_quantity = payload.get("quantity")
    try:
        requested_item_id = int(requested_item_id) if requested_item_id is not None else None
        requested_quantity = int(requested_quantity) if requested_quantity is not None else None
    except (TypeError, ValueError):
        return jsonify(error="Quantidade de retirada inválida."), 400
    sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale or not sale["ready_for_delivery"] or sale["delivered_at"]:
        return jsonify(error="Pedido não encontrado ou já entregue."), 409
    item_rows = db.execute(
        """SELECT si.id,si.quantity,p.name,
                  COALESCE((SELECT SUM(sid.quantity) FROM sale_item_deliveries sid WHERE sid.sale_item_id=si.id),0) delivered_quantity
           FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=? ORDER BY si.id""",
        (sale_id,),
    ).fetchall()
    if not item_rows:
        return jsonify(error="O pedido não possui itens para entregar."), 409
    # Pedidos vinculados a eventos/festas são entregues integralmente. Eles
    # não participam do fluxo de retirada parcial usado nas compras normais.
    if sale["event_id"] and requested_item_id is not None:
        return jsonify(error="Pedidos de eventos devem ser entregues integralmente. Use 'Entregar todos os produtos'."), 400
    if requested_item_id is None:
        deliver_plan = {item["id"]: max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0)) for item in item_rows}
    else:
        deliver_plan = {item["id"]: (requested_quantity or 0) if item["id"] == requested_item_id else 0 for item in item_rows}
    deliver_plan = {item_id: quantity for item_id, quantity in deliver_plan.items() if quantity > 0}
    if not deliver_plan:
        return jsonify(error="Não há unidades pendentes para entregar."), 409
    item_by_id = {item["id"]: item for item in item_rows}
    for item_id, quantity in deliver_plan.items():
        pending = int(item_by_id[item_id]["quantity"] or 0) - int(item_by_id[item_id]["delivered_quantity"] or 0)
        if quantity > pending:
            return jsonify(error=f"A quantidade pendente de {item_by_id[item_id]['name']} é {pending}."), 409
    try:
        with db:
            if sale["payment_status"] == "pending_cash" and not sale["paid"]:
                db.execute("UPDATE sales SET paid=1,payment_status='approved',paid_at=COALESCE(paid_at,CURRENT_TIMESTAMP) WHERE id=?", (sale_id,))
            for item_id, quantity in deliver_plan.items():
                db.execute("INSERT INTO sale_item_deliveries(sale_item_id,quantity,delivered_by) VALUES(?,?,?)", (item_id, quantity, g.user["id"]))
            delivered_totals = db.execute(
                """SELECT si.quantity,COALESCE(SUM(sid.quantity),0) delivered_quantity
                   FROM sale_items si LEFT JOIN sale_item_deliveries sid ON sid.sale_item_id=si.id
                   WHERE si.sale_id=? GROUP BY si.id,si.quantity""", (sale_id,)
            ).fetchall()
            fully_delivered = all(int(item["delivered_quantity"] or 0) >= int(item["quantity"] or 0) for item in delivered_totals)
            if fully_delivered:
                db.execute("UPDATE sales SET delivered_at=CURRENT_TIMESTAMP,delivered_by=? WHERE id=?", (g.user["id"], sale_id))
    except Exception as exc:
        current_app.logger.error("Erro ao registrar retirada parcial do pedido %s: %s", sale_id, exc)
        return jsonify(error="Não foi possível registrar a retirada."), 500
    delivered_sale = db.execute(
        """SELECT s.id,s.player_id,s.delivered_at,s.guest_name,p.name,p.war_name
           FROM sales s LEFT JOIN players p ON p.id=s.player_id WHERE s.id=?""",
        (sale_id,),
    ).fetchone()
    if delivered_sale:
        display_name = delivered_sale["guest_name"] or delivered_sale["war_name"] or delivered_sale["name"] or f"Convidado #{sale_id}"
        delivery_time = db.execute("SELECT MAX(delivered_at) delivered_at FROM sale_item_deliveries sid JOIN sale_items si ON si.id=sid.sale_item_id WHERE si.sale_id=?", (sale_id,)).fetchone()["delivered_at"]
        delivered_at = brdate(delivery_time)
        current = delivery_order_data(db, db.execute(
            """SELECT s.*,p.name player_name,p.war_name,p.thumbnail_data player_thumbnail_data,u.name delivered_by_name
               FROM sales s LEFT JOIN players p ON p.id=s.player_id LEFT JOIN users u ON u.id=s.delivered_by WHERE s.id=?""", (sale_id,)
        ).fetchone())
        delivered_items = [{"name": item["name"], "quantity": deliver_plan[item["id"]]} for item in item_rows if item["id"] in deliver_plan]
        remaining_items = [{"name": item["name"], "quantity": item["pending_quantity"]} for item in current["items"] if item["pending_quantity"] > 0]
        delivered_total = sum(item["quantity"] for item in delivered_items)
        send_player_push_once(
            db,
            delivered_sale["player_id"],
            "pedido_retirada",
            f"{sale_id}-{delivery_time}",
            "Retirada confirmada",
            f"{('Pedido totalmente entregue' if not remaining_items else 'Retirada parcial registrada')}. {delivered_total} item(ns) retirado(s) em {delivered_at}." + (f" Restam {sum(item['quantity'] for item in remaining_items)} item(ns)." if remaining_items else ""),
            url_for("auth.my_purchases"),
        )
        if delivered_sale["player_id"]:
            send_delivery_update(db, sale_id, delivered_items, remaining_items, current_app.config.get("GMAIL_SMTP_USER", ""), current_app.config.get("GMAIL_APP_PASSWORD", ""))
    receipt_status = send_purchase_receipt(
        db, sale_id, current_app.config.get("GMAIL_SMTP_USER", ""),
        current_app.config.get("GMAIL_APP_PASSWORD", ""),
    ) if delivered_sale and delivered_sale["player_id"] else "skipped_guest"
    return jsonify(ok=True, sale_id=sale_id, partial=bool(remaining_items), remaining_items=remaining_items, receipt_status=receipt_status)

@bp.post("/orders/<int:sale_id>/restore-delivery")
@roles_allowed("manager")
def restore_delivered_order(sale_id):
    """Reopen an order that was marked as fully delivered by mistake."""
    payload = request.get_json(silent=True) or {}
    reason = " ".join((payload.get("reason") or "").split())
    if len(reason) < 5:
        return jsonify(error="Informe uma justificativa com pelo menos 5 caracteres."), 400
    reason = reason[:500]

    db = get_db()
    sale = db.execute(
        "SELECT id,delivered_at,paid,payment_status,ready_for_delivery FROM sales WHERE id=?",
        (sale_id,),
    ).fetchone()
    if not sale or not sale["delivered_at"]:
        return jsonify(error="Pedido não encontrado ou não está totalmente entregue."), 409

    try:
        with db:
            removed = db.execute(
                "DELETE FROM sale_item_deliveries WHERE sale_item_id IN "
                "(SELECT id FROM sale_items WHERE sale_id=?)",
                (sale_id,),
            ).rowcount
            updated = db.execute(
                "UPDATE sales SET delivered_at=NULL,delivered_by=NULL WHERE id=? AND delivered_at IS NOT NULL",
                (sale_id,),
            )
            if updated.rowcount != 1:
                raise RuntimeError("pedido deixou de estar entregue durante a restauração")
    except Exception as exc:
        current_app.logger.error(
            "ORDER_DELIVERY_RESTORE_ERROR sale_id=%s manager_id=%s exception_type=%s",
            sale_id, g.user["id"], type(exc).__name__,
        )
        return jsonify(error="Não foi possível restaurar o pedido."), 500

    current_app.logger.warning(
        "ORDER_DELIVERY_RESTORED sale_id=%s manager_id=%s removed_deliveries=%s reason=%s",
        sale_id, g.user["id"], removed, reason,
    )
    return jsonify(ok=True, sale_id=sale_id)

@bp.post("/orders/<int:sale_id>/cancel")
@roles_allowed("manager", "staff")
def cancel_cash_order(sale_id):
    db = get_db()
    reason = (request.form.get("reason") or (request.get_json(silent=True) or {}).get("reason") or "").strip()
    # Compatibilidade com integrações antigas: a interface atual sempre envia
    # uma justificativa, mas registros legados recebem um motivo auditável.
    if len(reason) < 5:
        reason = "Cancelamento registrado pela atendente (sem justificativa informada)."
    reason = reason[:500]
    try:
        items = db.execute("SELECT product_id FROM sale_items WHERE sale_id=?", (sale_id,)).fetchall()
        with db:
            updated = db.execute(
                """UPDATE sales SET payment_status='canceled',ready_for_delivery=0
                   WHERE id=? AND payment_method='Dinheiro' AND paid=0
                   AND payment_status='pending_cash' AND delivered_at IS NULL""",
                (sale_id,),
            )
            if updated.rowcount != 1:
                return jsonify(error="Pedido em dinheiro não encontrado ou já finalizado."), 409
            db.execute(
                "INSERT INTO sale_cancellations(sale_id,reason,canceled_by) VALUES(?,?,?)",
                (sale_id, reason, g.user["id"]),
            )
            restore_reserved_stock(db, sale_id)
        notify_low_stock(db, [item["product_id"] for item in items])
    except Exception as exc:
        current_app.logger.error(f"Erro ao cancelar pedido em dinheiro {sale_id}: {exc}")
        return jsonify(error="Não foi possível cancelar o pedido."), 500
    return jsonify(ok=True, sale_id=sale_id, reason=reason)


@bp.get("/sales/<int:sale_id>/receipt")
@roles_allowed("manager", "staff", "client")
def receipt(sale_id):
    db = get_db()
    sale = db.execute(
        """SELECT s.*,p.name player_name,p.war_name,p.cpf,p.email,e.name event_name
           FROM sales s LEFT JOIN players p ON p.id=s.player_id LEFT JOIN bar_events e ON e.id=s.event_id WHERE s.id=?""",
        (sale_id,),
    ).fetchone()
    if not sale or (g.user["role"] == "client" and sale["player_id"] != g.user["player_id"]):
        return "Comprovante não encontrado.", 404
    if not sale["paid"] and sale["payment_status"] != "approved":
        return "O comprovante estará disponível após a confirmação do pagamento.", 409
    items = db.execute(
        """SELECT i.id item_id,i.quantity,i.unit_price_cents,p.name product_name,
                  COALESCE((SELECT SUM(sid.quantity) FROM sale_item_deliveries sid WHERE sid.sale_item_id=i.id),0) delivered_quantity
           FROM sale_items i JOIN products p ON p.id=i.product_id
           WHERE i.sale_id=? ORDER BY i.id""",
        (sale_id,),
    ).fetchall()
    if sale["delivered_at"] and items and not any(int(item["delivered_quantity"] or 0) > 0 for item in items):
        items = [dict(item, delivered_quantity=item["quantity"]) for item in items]
    receipt_items = []
    for item in items:
        entry = dict(item)
        entry["pending_quantity"] = max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0))
        receipt_items.append(entry)
    return render_template("purchase_receipt.html", sale=sale, items=receipt_items)

@bp.get("/pix/qrcode")
def pix_qrcode():
    try:
        if not require_pix_access_token():
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
        event_id = int(body.get("event_id") or 0) or None
        guest_name = str(body.get("guest_name") or "").strip()
        player_id = int(body.get("player_id")) if body.get("player_id") else None
        if event_id and not guest_name:
            raise ValueError("Informe o nome do convidado.")
        if not event_id and not player_id:
            raise ValueError("Selecione o peladeiro ou o evento.")
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
    event = db.execute("SELECT id,name FROM bar_events WHERE id=? AND status='open'", (event_id,)).fetchone() if event_id else None
    player = db.execute("SELECT id,email FROM players WHERE id=? AND active=1", (player_id,)).fetchone() if player_id else None
    placeholders = ",".join("?" for _ in requested)
    products = db.execute(
        f"SELECT id,name,price_cents,cost_cents,stock FROM products WHERE active=1 AND id IN ({placeholders})",
        tuple(requested),
    ).fetchall()
    products_by_id = {product["id"]: product for product in products}
    if (event_id and not event) or (not event_id and not player) or len(products_by_id) != len(requested):
        return jsonify(error="Peladeiro, evento ou produto inválido."), 400
    payer_email = str(player["email"] or "").strip().lower() if player else str(current_app.config.get("GMAIL_SMTP_USER") or "").strip().lower()
    if "@" not in payer_email:
        return jsonify(error="Configure um e-mail válido para gerar o Pix do evento." if event_id else "Cadastre um e-mail válido para o peladeiro antes de gerar o Pix."), 400
    for product_id, quantity in requested.items():
        if products_by_id[product_id]["stock"] < quantity:
            return jsonify(error=f"Estoque insuficiente de {products_by_id[product_id]['name']}."), 409

    total_cents = sum(products_by_id[product_id]["price_cents"] * quantity for product_id, quantity in requested.items())
    external_reference = f"evento_{uuid.uuid4().hex}" if event_id else f"pelada_{uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    try:
        with db:
            sale_cursor = db.execute(
                """INSERT INTO sales(player_id,event_id,guest_name,payment_method,total_cents,paid,payment_status,external_reference,idempotency_key,notes)
                   VALUES(?,?,?,? ,?,'0','creating',?,?,?)""",
                (player_id, event_id, guest_name, "Pix", total_cents, external_reference, idempotency_key, str(body.get("notes") or "").strip()),
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
        notify_low_stock(db, requested.keys())
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
            topup = db.execute(
                "SELECT * FROM bar_credit_topups WHERE mercadopago_order_id=? OR external_reference=?",
                (str(data_id or ""), notification_data.get("external_reference")),
            ).fetchone()
            if topup:
                order = notification_data
                if not order.get("status"):
                    access_token, _ = mercadopago_config()
                    if not access_token:
                        return "", 503
                    order = get_order(access_token, str(data_id))
                if order.get("status") == "processed" and order.get("status_detail") == "accredited":
                    with db:
                        approve_topup(db, topup, order_payment_id(order))
                elif order.get("status") in ("expired", "canceled"):
                    db.execute("UPDATE bar_credit_topups SET payment_status=? WHERE id=? AND paid=0", (order["status"], topup["id"]))
                    db.commit()
                return "", 200
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
