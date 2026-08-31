import uuid
from datetime import date, datetime
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
from src.services.bar_credits import (
    approve_topup,
    available_balance as available_credit_balance,
    consume as consume_credit,
    consume_reservation,
    credit_cash_change,
    low_balance_threshold,
    notify_low_balance,
    release_reservation,
    reserve_credit,
)
from src.services.pending_delivery_pdf import build_pending_delivery_pdf
from src.services.sports_supplier_pdf import build_sports_supplier_pdf
from src.services.notification_outbox import enqueue_sports_available_event
from src.catalog import SPORTS_MATERIAL_CATEGORY

bp = Blueprint("sales", __name__)
PIX_TOKEN_MAX_AGE = 60 * 60
SPORTS_FULFILLMENT_TRANSITIONS = {
    ("ready", "reserved"): "delivered",
    ("backorder", "requested"): "in_production",
    ("backorder", "in_production"): "available",
    ("backorder", "available"): "delivered",
}
SPORTS_FULFILLMENT_LABELS = {
    "reserved": "Reservado",
    "requested": "Solicitado",
    "in_production": "Em produção",
    "available": "Disponível para retirada",
    "delivered": "Entregue",
    "cancelled": "Cancelado",
}


def _sports_item_ids(payload):
    values = payload.getlist("sale_item_ids") if hasattr(payload, "getlist") else payload.get("sale_item_ids", [])
    if not isinstance(values, (list, tuple)):
        values = [values]
    try:
        result = sorted({int(value) for value in values if str(value).strip()})
    except (TypeError, ValueError) as exc:
        raise ValueError("Selecione encomendas válidas.") from exc
    if not result:
        raise ValueError("Selecione ao menos uma encomenda.")
    return result


def _sports_arrival_payload(item):
    size = str(item["variant_size"] or "Único")
    return {
        "player_id": int(item["player_id"]),
        "sale_item_id": int(item["sale_item_id"]),
        "title": "🎉 Seu produto chegou!",
        "body": f"{item['product_name']} · tamanho {size} está disponível para pagamento e retirada.",
        "url": url_for("auth.my_purchases"),
    }

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
    if not current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True):
        return None, None
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
    items = db.execute(
        """SELECT si.product_id,si.quantity,d.variant_id,d.order_mode,r.status reservation_status
           FROM sale_items si
           LEFT JOIN sports_sale_item_details d ON d.sale_item_id=si.id
           LEFT JOIN sports_stock_reservations r ON r.sale_item_id=si.id
           WHERE si.sale_id=?""",
        (sale_id,),
    ).fetchall()
    for item in items:
        if item["variant_id"]:
            if item["order_mode"] == "ready" and item["reservation_status"] == "reserved":
                released = db.execute(
                    """UPDATE sports_stock_reservations
                       SET status='released',updated_at=CURRENT_TIMESTAMP
                       WHERE sale_item_id IN (SELECT id FROM sale_items WHERE sale_id=?)
                         AND variant_id=? AND status='reserved'""",
                    (sale_id, item["variant_id"]),
                )
                if released.rowcount:
                    db.execute(
                        """UPDATE sports_product_variants
                           SET stock=stock+?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (item["quantity"], item["variant_id"]),
                    )
        else:
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

    reservation = db.execute(
        "SELECT amount_cents,status,expires_at FROM bar_credit_reservations WHERE sale_id=?",
        (sale["id"],),
    ).fetchone()
    reserved_cents = int(reservation["amount_cents"] or 0) if reservation else 0
    expected_external_cents = int(sale["total_cents"] or 0) - reserved_cents

    if status == "processed" and detail == "accredited" and paid_cents == expected_external_cents:
        if int(sale["paid"] or 0) and sale["payment_status"] == "approved":
            return "approved"
        if reservation and reservation["status"] == "released":
            return sale["payment_status"]
        if reservation and reservation["status"] == "reserved":
            active = db.execute(
                """SELECT 1 FROM bar_credit_reservations
                   WHERE sale_id=? AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
                (sale["id"],),
            ).fetchone()
            if not active:
                db.execute(
                    """UPDATE sales SET payment_status='credit_reservation_expired',
                       mercadopago_payment_id=? WHERE id=? AND paid=0""",
                    (payment_id, sale["id"]),
                )
                db.commit()
                return "credit_reservation_expired"
        with db:
            if reservation and reservation["status"] == "reserved":
                consume_reservation(db, sale["id"])
            db.execute(
                """UPDATE sports_stock_reservations SET status='consumed',updated_at=CURRENT_TIMESTAMP
                   WHERE sale_item_id IN (SELECT id FROM sale_items WHERE sale_id=?)
                     AND status='reserved'""",
                (sale["id"],),
            )
            db.execute(
                """UPDATE sales SET paid=1,payment_status='approved',mercadopago_payment_id=?,
                   paid_at=CURRENT_TIMESTAMP,ready_for_delivery=1
                   WHERE id=? AND paid=0""",
                (payment_id, sale["id"]),
            )
        return "approved"

    if status == "refunded" and sale["paid"]:
        db.execute(
            "UPDATE sales SET paid=0,payment_status='refunded',mercadopago_payment_id=? WHERE id=?",
            (payment_id, sale["id"]),
        )
        db.commit()
        return "refunded"

    terminal_statuses = {"expired", "canceled", "failed"}
    if status in terminal_statuses:
        with db:
            updated = db.execute(
                "UPDATE sales SET paid=0,payment_status=?,mercadopago_payment_id=? WHERE id=? AND paid=0 AND payment_status IN ('creating','pending')",
                (status, payment_id, sale["id"]),
            )
            if updated.rowcount:
                if reservation and reservation["status"] == "reserved":
                    release_reservation(db, sale["id"])
                restore_reserved_stock(db, sale["id"])
        return status

    return sale["payment_status"]

def _clean_sports_text(value, limit, label):
    value = " ".join(str(value or "").strip().split())
    if len(value) > limit:
        raise ValueError(f"{label} deve ter no máximo {limit} caracteres.")
    return value

def _create_sports_sale(db):
    if request.form.get("sale_type", "player").strip().lower() == "event" or request.form.get("event_id"):
        raise ValueError("Material Esportivo não está disponível para Convidado / Evento.")
    method = request.form.get("payment_method", "")
    if method == "Pix":
        raise ValueError("Pagamento Pix para Material Esportivo será disponibilizado em breve.")
    if method not in {"Dinheiro", "Créditos"}:
        raise ValueError("Material Esportivo aceita Dinheiro ou Créditos nesta etapa.")
    player_id = int(g.user["player_id"] or 0) if g.user["role"] == "client" else int(request.form.get("player_id") or 0)
    if not player_id:
        raise ValueError("Selecione o peladeiro.")
    raw_items = zip(request.form.getlist("product_id"), request.form.getlist("variant_id"),
                    request.form.getlist("quantity"), request.form.getlist("custom_name"),
                    request.form.getlist("custom_number"), request.form.getlist("order_mode"))
    items = []
    for product_id, variant_id, quantity, custom_name, custom_number, order_mode in raw_items:
        quantity = int(quantity or 0)
        if quantity > 0:
            items.append((int(product_id), int(variant_id), quantity,
                          _clean_sports_text(custom_name, 40, "Nome personalizado"),
                          _clean_sports_text(custom_number, 10, "Número"), order_mode))
    if not items:
        raise ValueError("Escolha ao menos um material esportivo.")
    variant_ids = [item[1] for item in items]
    placeholders = ",".join("?" for _ in variant_ids)
    rows = db.execute(f"""SELECT v.id variant_id,v.product_id,v.size,v.stock,v.active variant_active,
        p.name,p.price_cents,p.cost_cents,p.active product_active,p.category,
        c.allow_custom_name,c.allow_custom_number,c.allow_backorder,c.ready_sale_enabled
        FROM sports_product_variants v JOIN products p ON p.id=v.product_id
        JOIN sports_product_config c ON c.product_id=p.id WHERE v.id IN ({placeholders})""",
        tuple(variant_ids)).fetchall()
    by_variant = {row["variant_id"]: row for row in rows}
    if len(by_variant) != len(set(variant_ids)):
        raise ValueError("Uma variante selecionada não está mais disponível.")
    total = 0
    for product_id, variant_id, quantity, custom_name, custom_number, order_mode in items:
        row = by_variant[variant_id]
        if (row["product_id"] != product_id or not row["product_active"] or
                not row["variant_active"] or row["category"] != SPORTS_MATERIAL_CATEGORY):
            raise ValueError("Produto ou tamanho esportivo inválido.")
        if custom_name and not row["allow_custom_name"]:
            raise ValueError("Este produto não permite nome personalizado.")
        if custom_number and not row["allow_custom_number"]:
            raise ValueError("Este produto não permite número personalizado.")
        if order_mode not in {"ready", "backorder"}:
            raise ValueError("Escolha pronta entrega ou encomenda.")
        if order_mode == "backorder" and not row["allow_backorder"]:
            raise ValueError("Este produto não permite encomenda.")
        if order_mode == "ready" and not row["ready_sale_enabled"]:
            raise ValueError("Este produto está disponível somente por encomenda.")
        total += row["price_cents"] * quantity
    modes = {item[5] for item in items}
    if len(modes) != 1:
        raise ValueError("Finalize pronta entrega e encomenda em pedidos separados.")
    backorder_request = modes == {"backorder"}
    cash_pending = method == "Dinheiro" and not backorder_request
    with db:
        sale = db.execute("""INSERT INTO sales(player_id,payment_method,total_cents,paid,payment_status,paid_at,ready_for_delivery,notes)
            VALUES(?,?,?,?,?,CASE WHEN ?=1 THEN CURRENT_TIMESTAMP ELSE NULL END,0,?)""",
            (player_id, "Dinheiro" if backorder_request else method, total,
             0 if cash_pending or backorder_request else 1,
             "requested" if backorder_request else ("pending_cash" if cash_pending else "approved"),
             0 if cash_pending or backorder_request else 1,
             request.form.get("notes", "").strip()))
        for product_id, variant_id, quantity, custom_name, custom_number, order_mode in items:
            row = by_variant[variant_id]
            if order_mode == "ready":
                updated = db.execute("""UPDATE sports_product_variants SET stock=stock-?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND product_id=? AND active AND stock>=?""",
                    (quantity, variant_id, product_id, quantity))
                if updated.rowcount != 1:
                    raise ValueError(f"Estoque insuficiente para {row['name']} — {row['size']}.")
            sale_item = db.execute("INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                (sale.lastrowid, product_id, quantity, row["price_cents"], row["cost_cents"]))
            db.execute("""INSERT INTO sports_sale_item_details
                (sale_item_id,variant_id,variant_size,custom_name,custom_number,order_mode,fulfillment_status)
                VALUES(?,?,?,?,?,?,?) RETURNING sale_item_id""",
                (sale_item.lastrowid, variant_id, row["size"], custom_name, custom_number,
                 order_mode, "reserved" if order_mode == "ready" else "requested"))
        if method == "Créditos" and not backorder_request:
            if g.user["role"] != "client":
                raise ValueError("Somente o peladeiro pode pagar com créditos.")
            consume_credit(db, player_id, total, sale.lastrowid, g.user["id"])
    return sale.lastrowid

@bp.route("/sale", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "client")
def sale():
    db = get_db()
    if request.method == "POST":
        if request.form.get("department") == "sports":
            try:
                sale_id = _create_sports_sale(db)
                flash(f"Pedido esportivo #{sale_id} registrado com sucesso!", "success")
                return redirect(url_for("sales.sale", catalog="sports", cart_cleared="sports"), code=303)
            except ValueError as exc:
                flash(str(exc), "danger")
            except Exception as exc:
                current_app.logger.error("SPORTS_SALE_ERROR exception_type=%s", type(exc).__name__)
                flash("Erro interno ao processar a compra esportiva.", "danger")
            return redirect(url_for("sales.sale", catalog="sports"), code=303)
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
            use_bar_credit = request.form.get("use_bar_credit") == "1"
            if method == "Pix" and mercadopago_enabled():
                raise ValueError("Para pagamentos Pix, gere o QR Code e aguarde a confirmação automática.")
            if sale_type == "event" and method not in ("Pix", "Dinheiro", "Débito", "Cortesia"):
                raise ValueError("Vendas de evento não podem usar créditos de peladeiro.")
            if g.user["role"] == "client" and method not in ("Pix", "Dinheiro", "Créditos"):
                raise ValueError("Clientes podem registrar pagamentos somente em Pix ou Dinheiro.")

            credit_amount = 0
            if use_bar_credit and method == "Dinheiro" and player_id:
                credit_amount = min(available_credit_balance(db, player_id), total)
                if credit_amount == total:
                    method = "Créditos"
            
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
                    if not player_id:
                        raise ValueError("Selecione um peladeiro para pagar com créditos.")
                    paid = 1
                    payment_status = "approved"
                    db.execute("UPDATE sales SET paid=1,payment_status='approved',paid_at=CURRENT_TIMESTAMP WHERE id=?", (cur.lastrowid,))
                elif credit_amount:
                    reserve_credit(db, player_id, cur.lastrowid, credit_amount)
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
           WHERE p.active=1 AND p.stock>0 AND p.category<>?
           GROUP BY p.id""", (SPORTS_MATERIAL_CATEGORY,)
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
    sports_rows = db.execute("""SELECT p.id,p.name,p.category,p.price_cents,p.cost_cents,p.thumbnail_data,
        t.name sports_type,t.code sports_type_code,c.allow_custom_name,c.allow_custom_number,c.allow_backorder,
        c.ready_sale_enabled,
        v.id variant_id,v.size variant_size,v.stock variant_stock,v.min_stock variant_min_stock,v.active variant_active
        FROM products p JOIN sports_product_config c ON c.product_id=p.id
        JOIN sports_material_types t ON t.id=c.type_id
        JOIN sports_product_variants v ON v.product_id=p.id AND v.active
        WHERE p.active=1 AND p.category=? ORDER BY p.name,v.id""", (SPORTS_MATERIAL_CATEGORY,)).fetchall()
    sports_products = {}
    for row in sports_rows:
        product = sports_products.setdefault(row["id"], {"id":row["id"], "name":row["name"],
            "category":row["category"], "price_cents":row["price_cents"], "cost_cents":row["cost_cents"],
            "thumbnail_data":row["thumbnail_data"], "stock":0, "min_stock":0, "sold_quantity":0,
            "group":SPORTS_MATERIAL_CATEGORY, "sports_type":row["sports_type"],
            "single_variant":row["sports_type_code"] == "commemorative_coin",
            "allow_custom_name":bool(row["allow_custom_name"]),
            "allow_custom_number":bool(row["allow_custom_number"]),
            "allow_backorder":bool(row["allow_backorder"]),
            "ready_sale_enabled":bool(row["ready_sale_enabled"]), "variants":[]})
        product["variants"].append({"id":row["variant_id"], "size":row["variant_size"],
            "stock":int(row["variant_stock"] or 0), "min_stock":int(row["variant_min_stock"] or 0),
            "active":bool(row["variant_active"])})
        product["stock"] += int(row["variant_stock"] or 0)
        product["min_stock"] += int(row["variant_min_stock"] or 0)
    product_data.extend(sports_products.values())
    product_data.sort(key=lambda product: (-int(product.get("sold_quantity") or 0), (product.get("category") or "").lower(), (product.get("name") or "").lower()))
    product_rows = product_data
    player_available_credits = {
        int(player["id"]): available_credit_balance(db, player["id"])
        for player in player_rows
    }
    client_credit_balance = player_available_credits.get(int(g.user["player_id"] or 0), 0) if g.user["role"] == "client" else 0
    open_events = db.execute(
        "SELECT id,name,event_date FROM bar_events WHERE status='open' ORDER BY event_date DESC,id DESC"
    ).fetchall() if g.user["role"] in ("manager", "staff") else []
    product_groups = [group for group in ("Bebidas", "Alimentos", "Salgados", "Outros", SPORTS_MATERIAL_CATEGORY) if any(product["group"] == group for product in product_data)]
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
            "available_credit_cents": player_available_credits[int(player["id"])],
        } for player in player_rows],
        pix_token=pix_access_token(g.user),
        mercadopago_enabled=mercadopago_enabled(),
        external_payments_enabled=current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True),
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
        external_payments_enabled=current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True),
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
    # Ensure templates always receive a department value. Default to 'bar'.
    department = request.args.get('department', 'bar')
    if department not in ('bar', 'sports'):
        department = 'bar'
    return render_template("orders.html", department=department)

@bp.get("/material-esportivo/vendas")
@roles_allowed("manager", "staff")
def sports_material_sales():
    db = get_db()
    default_status = "reserved" if g.user["role"] == "staff" else ""
    status = request.args.get("status", default_status).strip()
    if status not in {"", *SPORTS_FULFILLMENT_LABELS}:
        status = default_status
    search = request.args.get("q", "").strip()[:100]
    where = ["p.category=?"]
    params = [SPORTS_MATERIAL_CATEGORY]
    if status:
        where.append("d.fulfillment_status=?")
        params.append(status)
    if search:
        where.append("""(LOWER(p.name) LIKE LOWER(?)
                         OR LOWER(COALESCE(pl.name,'')) LIKE LOWER(?)
                         OR LOWER(COALESCE(pl.war_name,'')) LIKE LOWER(?))""")
        term = f"%{search}%"
        params.extend((term, term, term))
    items = db.execute(
        f"""SELECT si.id sale_item_id,si.sale_id,si.quantity,si.unit_price_cents,
                   p.id product_id,p.name product_name,p.thumbnail_data,t.name material_type,t.code material_type_code,
                   COALESCE(pl.war_name,pl.name,'Peladeiro') player_name,
                   s.payment_method,s.payment_status,s.paid,s.created_at,
                   d.variant_id,d.variant_size,d.custom_name,d.custom_number,d.order_mode,
                   d.fulfillment_status,d.cancellation_resolution,d.cancellation_reason,
                   d.delivered_at,u.name delivered_by_name,
                   r.status reservation_status
            FROM sports_sale_item_details d
            JOIN sale_items si ON si.id=d.sale_item_id
            JOIN sales s ON s.id=si.sale_id
            JOIN products p ON p.id=si.product_id
            JOIN sports_product_config config ON config.product_id=p.id
            JOIN sports_material_types t ON t.id=config.type_id
            LEFT JOIN players pl ON pl.id=s.player_id
            LEFT JOIN users u ON u.id=d.delivered_by
            LEFT JOIN sports_stock_reservations r ON r.sale_item_id=si.id
            WHERE {' AND '.join(where)}
            ORDER BY CASE d.fulfillment_status
                       WHEN 'available' THEN 1 WHEN 'reserved' THEN 2
                       WHEN 'in_production' THEN 3 WHEN 'requested' THEN 4 ELSE 5 END,
                     s.created_at DESC,si.id DESC""",
        tuple(params),
    ).fetchall()
    rows = []
    for item in items:
        row = dict(item)
        row["fulfillment_label"] = SPORTS_FULFILLMENT_LABELS[row["fulfillment_status"]]
        row["next_status"] = SPORTS_FULFILLMENT_TRANSITIONS.get(
            (row["order_mode"], row["fulfillment_status"])
        ) if row["order_mode"] == "ready" or row["fulfillment_status"] == "available" else None
        row["next_label"] = {
            "in_production": "Iniciar produção",
            "available": "Marcar como disponível",
            "delivered": "Registrar entrega",
        }.get(row["next_status"])
        rows.append(row)
    receiving_groups = {}
    for row in rows:
        if row["order_mode"] != "backorder" or row["fulfillment_status"] != "in_production":
            continue
        key = (int(row["product_id"]), int(row["variant_id"]))
        group = receiving_groups.setdefault(key, {
            "product_id": key[0], "variant_id": key[1],
            "product_name": row["product_name"], "variant_size": row["variant_size"],
            "items": [], "waiting_quantity": 0,
        })
        group["items"].append(row)
        group["waiting_quantity"] += int(row["quantity"] or 0)
    return render_template(
        "sports_orders.html", items=rows, status=status, search=search,
        status_labels=SPORTS_FULFILLMENT_LABELS,
        receiving_groups=list(receiving_groups.values()),
    )


@bp.get("/material-esportivo/vendas/fornecedor.pdf")
@roles_allowed("manager", "staff")
def sports_supplier_pdf():
    rows = get_db().execute(
        """SELECT si.quantity,p.name product_name,d.variant_size,d.custom_name,d.custom_number,
                  COALESCE(pl.war_name,pl.name,'Peladeiro') player_name
           FROM sports_sale_item_details d
           JOIN sale_items si ON si.id=d.sale_item_id
           JOIN sales s ON s.id=si.sale_id
           JOIN products p ON p.id=si.product_id
           LEFT JOIN players pl ON pl.id=s.player_id
           WHERE d.order_mode='backorder' AND d.fulfillment_status='requested'
           ORDER BY p.name,d.variant_size,player_name,si.id"""
    ).fetchall()
    return send_file(
        build_sports_supplier_pdf(rows), mimetype="application/pdf", as_attachment=False,
        download_name=f"encomendas-fornecedor-{local_today().isoformat()}.pdf",
    )


@bp.post("/material-esportivo/vendas/confirmar-envio")
@roles_allowed("manager", "staff")
def confirm_sports_supplier_send():
    payload = request.get_json(silent=True) or request.form
    try:
        item_ids = _sports_item_ids(payload)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    db = get_db()
    placeholders = ",".join("?" for _ in item_ids)
    rows = db.execute(
        f"""SELECT d.sale_item_id,d.fulfillment_status,d.order_mode,s.paid
            FROM sports_sale_item_details d JOIN sale_items si ON si.id=d.sale_item_id
            JOIN sales s ON s.id=si.sale_id WHERE d.sale_item_id IN ({placeholders})""",
        tuple(item_ids),
    ).fetchall()
    if len(rows) != len(item_ids) or any(row["order_mode"] != "backorder" or row["fulfillment_status"] != "requested" or row["paid"] for row in rows):
        return jsonify(error="Somente encomendas solicitadas e ainda não pagas podem ser enviadas."), 409
    with db:
        for item_id in item_ids:
            updated = db.execute(
                """UPDATE sports_sale_item_details SET fulfillment_status='in_production',updated_at=CURRENT_TIMESTAMP
                   WHERE sale_item_id=? AND fulfillment_status='requested'""", (item_id,),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Uma encomenda mudou durante a confirmação.")
            db.execute(
                """INSERT INTO sports_order_status_history(sale_item_id,from_status,to_status,changed_by,notes)
                   VALUES(?,'requested','in_production',?,'Envio ao fornecedor confirmado')""",
                (item_id, g.user["id"]),
            )
    return jsonify(ok=True, updated=len(item_ids))


@bp.post("/material-esportivo/vendas/receber")
@roles_allowed("manager", "staff")
def receive_sports_backorders():
    payload = request.get_json(silent=True) or request.form
    raw_groups = payload.get("groups") if hasattr(payload, "get") else None
    legacy_payload = raw_groups is None
    if raw_groups is None:
        # Compatibilidade com o recebimento simples anterior.
        raw_groups = [{
            "product_id": payload.get("product_id"),
            "variant_id": payload.get("variant_id"),
            "received_quantity": payload.get("received_quantity"),
            "sale_item_ids": payload.get("sale_item_ids", []),
        }]
    if not isinstance(raw_groups, list):
        return jsonify(error="Informe os grupos de recebimento por produto e variante."), 400
    groups = []
    seen_groups = set()
    seen_items = set()
    try:
        for raw in raw_groups:
            if not isinstance(raw, dict):
                raise ValueError
            quantity_value = raw.get("received_quantity")
            if quantity_value in (None, ""):
                continue
            received_quantity = int(quantity_value)
            if received_quantity < 0:
                raise ValueError
            if received_quantity == 0:
                continue
            product_id = int(raw.get("product_id") or 0)
            variant_id = int(raw.get("variant_id") or 0)
            item_values = raw.get("sale_item_ids") or []
            if not isinstance(item_values, (list, tuple)):
                item_values = [item_values]
            item_ids = sorted({int(value) for value in item_values if str(value).strip()})
            if (product_id <= 0 and not legacy_payload) or variant_id <= 0 or not item_ids:
                raise ValueError
            group_key = (product_id, variant_id)
            if group_key in seen_groups or any(item_id in seen_items for item_id in item_ids):
                raise ValueError
            seen_groups.add(group_key)
            seen_items.update(item_ids)
            groups.append({
                "product_id": product_id, "variant_id": variant_id,
                "received_quantity": received_quantity, "item_ids": item_ids,
            })
    except (TypeError, ValueError):
        return jsonify(error="Informe quantidades inteiras e seleções válidas para cada produto/tamanho."), 400
    if not groups:
        return jsonify(error="Informe quantidade maior que zero e selecione as encomendas atendidas em ao menos um grupo."), 400
    db = get_db()
    prepared = []
    for group in groups:
        variant = db.execute(
            "SELECT id,product_id,active FROM sports_product_variants WHERE id=?",
            (group["variant_id"],),
        ).fetchone()
        if legacy_payload and variant and group["product_id"] <= 0:
            group["product_id"] = int(variant["product_id"])
        placeholders = ",".join("?" for _ in group["item_ids"])
        rows = db.execute(
            f"""SELECT d.sale_item_id,d.variant_id,d.variant_size,d.custom_name,d.custom_number,
                       d.fulfillment_status,si.sale_id,si.product_id,si.quantity,
                       p.name product_name,s.player_id
                FROM sports_sale_item_details d JOIN sale_items si ON si.id=d.sale_item_id
                JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
                WHERE d.sale_item_id IN ({placeholders})""", tuple(group["item_ids"]),
        ).fetchall()
        allocated = sum(int(row["quantity"] or 0) for row in rows)
        if (not variant or not variant["active"] or variant["product_id"] != group["product_id"]
                or len(rows) != len(group["item_ids"])
                or any(row["product_id"] != group["product_id"]
                       or row["variant_id"] != group["variant_id"]
                       or row["fulfillment_status"] != "in_production" for row in rows)
                or allocated > group["received_quantity"]):
            return jsonify(error="Seleção incompatível com o produto, a variante, o estado ou a quantidade recebida."), 409
        prepared.append({
            **group, "rows": rows, "allocated": allocated,
            "excess": group["received_quantity"] - allocated,
        })
    try:
        with db:
            for group in prepared:
                for row in group["rows"]:
                    updated = db.execute(
                        """UPDATE sports_sale_item_details SET fulfillment_status='available',updated_at=CURRENT_TIMESTAMP
                           WHERE sale_item_id=? AND fulfillment_status='in_production'""", (row["sale_item_id"],),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("Uma encomenda mudou durante o recebimento.")
                    db.execute(
                        """INSERT INTO sports_order_status_history(sale_item_id,from_status,to_status,changed_by,notes)
                           VALUES(?,'in_production','available',?,'Recebimento do fornecedor')""",
                        (row["sale_item_id"], g.user["id"]),
                    )
                    enqueue_sports_available_event(db, row["sale_id"], row["sale_item_id"], _sports_arrival_payload(row))
                if group["excess"]:
                    updated_stock = db.execute(
                        """UPDATE sports_product_variants SET stock=stock+?,updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND product_id=? AND active=1""",
                        (group["excess"], group["variant_id"], group["product_id"]),
                    )
                    if updated_stock.rowcount != 1:
                        raise RuntimeError("Variante esportiva indisponível.")
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(
        ok=True, groups=len(prepared),
        available=sum(len(group["rows"]) for group in prepared),
        allocated_quantity=sum(group["allocated"] for group in prepared),
        stock_excess=sum(group["excess"] for group in prepared),
        results=[{
            "product_id": group["product_id"], "variant_id": group["variant_id"],
            "available": len(group["rows"]), "allocated_quantity": group["allocated"],
            "stock_excess": group["excess"],
        } for group in prepared],
    )


@bp.post("/material-esportivo/vendas/<int:sale_item_id>/cancelar")
@roles_allowed("manager", "staff")
def cancel_sports_backorder(sale_item_id):
    payload = request.get_json(silent=True) or request.form
    reason = " ".join(str(payload.get("reason") or "").split())[:500]
    if not reason:
        return jsonify(error="Informe o motivo do cancelamento."), 400
    db = get_db()
    item = db.execute(
        """SELECT d.*,si.sale_id,si.quantity,s.paid,s.payment_status
           FROM sports_sale_item_details d JOIN sale_items si ON si.id=d.sale_item_id
           JOIN sales s ON s.id=si.sale_id WHERE d.sale_item_id=?""", (sale_item_id,),
    ).fetchone()
    if not item:
        return jsonify(error="Encomenda não encontrada."), 404
    if item["paid"] or item["payment_status"] == "approved":
        return jsonify(error="Pedido já pago. O cancelamento exige tratamento/estorno manual."), 409
    if item["fulfillment_status"] == "cancelled":
        return jsonify(ok=True, already_cancelled=True, resolution=item["cancellation_resolution"])
    if item["order_mode"] != "backorder" or item["fulfillment_status"] not in {"requested", "in_production", "available"}:
        return jsonify(error="Este item não pode ser cancelado por este fluxo."), 409
    personalized = bool(item["custom_name"] or item["custom_number"])
    if item["fulfillment_status"] == "in_production":
        resolution = "admin_pending" if personalized else "awaiting_arrival"
    elif item["fulfillment_status"] == "available":
        resolution = "admin_pending" if personalized else "stocked"
    else:
        resolution = "none"
    current = item["fulfillment_status"]
    with db:
        updated = db.execute(
            """UPDATE sports_sale_item_details SET fulfillment_status='cancelled',canceled_at=CURRENT_TIMESTAMP,
               canceled_by=?,cancellation_reason=?,cancellation_resolution=?,updated_at=CURRENT_TIMESTAMP
               WHERE sale_item_id=? AND fulfillment_status=?""",
            (g.user["id"], reason, resolution, sale_item_id, current),
        )
        if updated.rowcount != 1:
            return jsonify(error="A encomenda mudou durante o cancelamento."), 409
        if current == "available" and resolution == "stocked":
            db.execute(
                "UPDATE sports_product_variants SET stock=stock+?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (item["quantity"], item["variant_id"]),
            )
        db.execute(
            """INSERT INTO sports_order_status_history(sale_item_id,from_status,to_status,changed_by,notes)
               VALUES(?,?,'cancelled',?,?)""", (sale_item_id, current, g.user["id"], reason),
        )
    return jsonify(ok=True, resolution=resolution)


@bp.post("/material-esportivo/vendas/<int:sale_item_id>/resolver-cancelamento")
@roles_allowed("manager")
def resolve_sports_cancellation(sale_item_id):
    payload = request.get_json(silent=True) or request.form
    action = str(payload.get("action") or "").strip()
    db = get_db()
    source = db.execute(
        """SELECT d.*,si.quantity,si.product_id FROM sports_sale_item_details d
           JOIN sale_items si ON si.id=d.sale_item_id WHERE d.sale_item_id=?""", (sale_item_id,),
    ).fetchone()
    if not source or source["fulfillment_status"] != "cancelled":
        return jsonify(error="Pendência cancelada não encontrada."), 404
    if source["cancellation_resolution"] in {"stocked", "reassigned"}:
        return jsonify(ok=True, already_resolved=True, resolution=source["cancellation_resolution"])
    personalized = bool(source["custom_name"] or source["custom_number"])
    if action == "stock":
        if personalized:
            return jsonify(error="Produto personalizado não pode entrar automaticamente no estoque comum."), 409
        with db:
            updated = db.execute(
                """UPDATE sports_sale_item_details SET cancellation_resolution='stocked',updated_at=CURRENT_TIMESTAMP
                   WHERE sale_item_id=? AND cancellation_resolution='awaiting_arrival'""", (sale_item_id,),
            )
            if updated.rowcount != 1:
                return jsonify(error="Esta pendência não está disponível para entrada em estoque."), 409
            db.execute("UPDATE sports_product_variants SET stock=stock+?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (source["quantity"], source["variant_id"]))
        return jsonify(ok=True, resolution="stocked")
    if action == "admin_pending":
        db.execute("UPDATE sports_sale_item_details SET cancellation_resolution='admin_pending',updated_at=CURRENT_TIMESTAMP WHERE sale_item_id=?", (sale_item_id,))
        db.commit()
        return jsonify(ok=True, resolution="admin_pending")
    if action == "reassign":
        if personalized:
            return jsonify(error="Produto personalizado não pode ser realocado automaticamente."), 409
        try:
            target_id = int(payload.get("target_sale_item_id") or 0)
        except (TypeError, ValueError):
            target_id = 0
        target = db.execute(
            """SELECT d.sale_item_id,d.variant_id,d.variant_size,d.fulfillment_status,
                      si.sale_id,p.name product_name,s.player_id
               FROM sports_sale_item_details d JOIN sale_items si ON si.id=d.sale_item_id
               JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id
               WHERE d.sale_item_id=?""", (target_id,),
        ).fetchone()
        if (not target or target["variant_id"] != source["variant_id"]
                or target["fulfillment_status"] != "in_production"):
            return jsonify(error="Escolha outra encomenda em produção da mesma variante."), 409
        with db:
            source_updated = db.execute(
                """UPDATE sports_sale_item_details SET cancellation_resolution='reassigned',updated_at=CURRENT_TIMESTAMP
                   WHERE sale_item_id=? AND cancellation_resolution='awaiting_arrival'""", (sale_item_id,),
            )
            target_updated = db.execute(
                """UPDATE sports_sale_item_details SET fulfillment_status='available',updated_at=CURRENT_TIMESTAMP
                   WHERE sale_item_id=? AND fulfillment_status='in_production'""", (target_id,),
            )
            if source_updated.rowcount != 1 or target_updated.rowcount != 1:
                raise RuntimeError("Uma das encomendas mudou durante a realocação.")
            db.execute(
                """INSERT INTO sports_order_status_history(sale_item_id,from_status,to_status,changed_by,notes)
                   VALUES(?,'in_production','available',?,'Unidade realocada manualmente de encomenda cancelada')""",
                (target_id, g.user["id"]),
            )
            enqueue_sports_available_event(db, target["sale_id"], target_id, _sports_arrival_payload(target))
        return jsonify(ok=True, resolution="reassigned", target_sale_item_id=target_id)
    return jsonify(error="Escolha uma resolução administrativa válida."), 400


@bp.post("/material-esportivo/vendas/<int:sale_item_id>/pagamento")
@roles_allowed("manager", "staff", "client")
def start_sports_backorder_payment(sale_item_id):
    payload = request.get_json(silent=True) or request.form
    method = str(payload.get("payment_method") or "").strip()
    if method not in {"Dinheiro", "Créditos", "Pix"}:
        return jsonify(error="Forma de pagamento inválida."), 400
    db = get_db()
    item = db.execute(
        """SELECT d.fulfillment_status,d.order_mode,si.sale_id,si.quantity,s.player_id,s.total_cents,
                  s.paid,s.payment_status,p.email
           FROM sports_sale_item_details d JOIN sale_items si ON si.id=d.sale_item_id
           JOIN sales s ON s.id=si.sale_id JOIN players p ON p.id=s.player_id
           WHERE d.sale_item_id=?""", (sale_item_id,),
    ).fetchone()
    if not item:
        return jsonify(error="Encomenda não encontrada."), 404
    if g.user["role"] == "client" and int(g.user["player_id"] or 0) != int(item["player_id"]):
        return jsonify(error="Você não pode pagar esta encomenda."), 403
    if item["order_mode"] != "backorder" or item["fulfillment_status"] != "available":
        return jsonify(error="O pagamento só é liberado depois que a encomenda estiver disponível."), 409
    if item["paid"]:
        return jsonify(ok=True, paid=True, already_paid=True, sale_id=item["sale_id"])
    sale_id = item["sale_id"]
    if method == "Créditos":
        try:
            with db:
                consume_credit(db, item["player_id"], item["total_cents"], sale_id, g.user["id"])
                db.execute("""UPDATE sales SET payment_method='Créditos',paid=1,payment_status='approved',
                           paid_at=CURRENT_TIMESTAMP,ready_for_delivery=1 WHERE id=? AND paid=0""", (sale_id,))
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True, paid=True, sale_id=sale_id)
    if method == "Dinheiro":
        db.execute("UPDATE sales SET payment_method='Dinheiro',payment_status='pending_cash' WHERE id=? AND paid=0", (sale_id,))
        db.commit()
        return jsonify(ok=True, paid=False, sale_id=sale_id, status="pending_cash")
    if not current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True):
        return jsonify(error="Pagamento Pix indisponível na homologação."), 403
    access_token, _ = mercadopago_config()
    if not access_token or "@" not in str(item["email"] or ""):
        return jsonify(error="Pix indisponível ou peladeiro sem e-mail válido."), 503
    external_reference = f"sports_backorder_{sale_id}_{uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    try:
        order = create_pix_order(access_token, external_reference, item["total_cents"], idempotency_key, item["email"])
        payments = (order.get("transactions") or {}).get("payments") or []
        qr_data = ((payments[0].get("payment_method") or {}).get("qr_code") if payments else None)
        if not order.get("id") or not qr_data:
            raise MercadoPagoError("O Mercado Pago não retornou o QR Code Pix.")
        db.execute("""UPDATE sales SET payment_method='Pix',payment_status='pending',external_reference=?,idempotency_key=?,
                   mercadopago_order_id=?,mercadopago_payment_id=? WHERE id=? AND paid=0""",
                   (external_reference, idempotency_key, order["id"], order_payment_id(order), sale_id))
        db.commit()
        return jsonify(ok=True, sale_id=sale_id, status="pending", payload=qr_data,
                       image=f"data:image/png;base64,{generate_qrcode_base64(qr_data)}",
                       status_url=url_for("sales.mercadopago_order_status", sale_id=sale_id))
    except Exception as exc:
        current_app.logger.error("Erro ao cobrar encomenda esportiva %s: %s", sale_id, exc)
        return jsonify(error=str(exc) if isinstance(exc, MercadoPagoError) else "Não foi possível criar a cobrança Pix."), 502

@bp.post("/material-esportivo/vendas/<int:sale_item_id>/status")
@roles_allowed("manager", "staff")
def update_sports_fulfillment(sale_item_id):
    payload = request.get_json(silent=True) or request.form
    target = str(payload.get("to_status") or "").strip()
    notes = " ".join(str(payload.get("notes") or "").split())[:500]
    db = get_db()
    item = db.execute(
        """SELECT d.sale_item_id,d.order_mode,d.fulfillment_status,d.delivered_at,d.delivered_by,
                  s.id sale_id,s.payment_method,s.payment_status,s.paid,r.status reservation_status
           FROM sports_sale_item_details d
           JOIN sale_items si ON si.id=d.sale_item_id
           JOIN sales s ON s.id=si.sale_id
           LEFT JOIN sports_stock_reservations r ON r.sale_item_id=d.sale_item_id
           WHERE d.sale_item_id=?""",
        (sale_item_id,),
    ).fetchone()
    if not item:
        return jsonify(error="Item esportivo não encontrado."), 404
    current = item["fulfillment_status"]
    expected = SPORTS_FULFILLMENT_TRANSITIONS.get((item["order_mode"], current))
    if current == "delivered" and target == "delivered":
        return jsonify(ok=True, already_delivered=True, sale_item_id=sale_item_id)
    if not target or target != expected:
        return jsonify(error="Transição operacional não permitida."), 409
    payment_status = (item["payment_status"] or "").lower()
    is_delivery = target == "delivered"
    if payment_status in {"failed", "expired", "canceled", "refunded"}:
        return jsonify(error="O estado do pagamento bloqueia esta operação."), 409
    if is_delivery and (not item["paid"] or payment_status != "approved"):
        return jsonify(error="A entrega só pode ser registrada após a confirmação do pagamento."), 409
    if is_delivery and item["reservation_status"] == "released":
        return jsonify(error="A reserva deste item foi liberada e ele não pode ser entregue."), 409
    if item["payment_method"] != "Dinheiro" and (not item["paid"] or payment_status != "approved"):
        return jsonify(error="O pagamento ainda não foi confirmado."), 409
    try:
        with db:
            if is_delivery:
                updated = db.execute(
                    """UPDATE sports_sale_item_details
                       SET fulfillment_status=?,delivered_at=CURRENT_TIMESTAMP,delivered_by=?,updated_at=CURRENT_TIMESTAMP
                       WHERE sale_item_id=? AND fulfillment_status=? AND delivered_at IS NULL""",
                    (target, g.user["id"], sale_item_id, current),
                )
            else:
                updated = db.execute(
                    """UPDATE sports_sale_item_details SET fulfillment_status=?,updated_at=CURRENT_TIMESTAMP
                       WHERE sale_item_id=? AND fulfillment_status=?""",
                    (target, sale_item_id, current),
                )
            if updated.rowcount != 1:
                raise ValueError("concurrent_transition")
            db.execute(
                """INSERT INTO sports_order_status_history
                   (sale_item_id,from_status,to_status,changed_by,notes) VALUES(?,?,?,?,?)""",
                (sale_item_id, current, target, g.user["id"], notes),
            )
    except ValueError as exc:
        if str(exc) == "concurrent_transition":
            latest = db.execute(
                "SELECT fulfillment_status FROM sports_sale_item_details WHERE sale_item_id=?",
                (sale_item_id,),
            ).fetchone()
            if latest and latest["fulfillment_status"] == "delivered" and target == "delivered":
                return jsonify(ok=True, already_delivered=True, sale_item_id=sale_item_id)
            return jsonify(error="O item foi atualizado por outro operador. Recarregue a página."), 409
        raise
    except Exception as exc:
        current_app.logger.error(
            "SPORTS_FULFILLMENT_ERROR sale_item_id=%s user_id=%s exception_type=%s",
            sale_item_id, g.user["id"], type(exc).__name__,
        )
        return jsonify(error="Não foi possível atualizar o pedido esportivo."), 500
    return jsonify(ok=True, sale_item_id=sale_item_id, from_status=current, to_status=target)

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

def delivery_order_data(db, sale, items_for_sales=None):
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
    if items_for_sales and sale.get("id") in items_for_sales:
        items = items_for_sales[sale.get("id")]
    else:
        items = db.execute(
            """SELECT si.id,si.quantity,p.name,
                      COALESCE((SELECT SUM(sid.quantity) FROM sale_item_deliveries sid WHERE sid.sale_item_id=si.id),0) delivered_quantity
               FROM sale_items si
               JOIN products p ON p.id=si.product_id WHERE si.sale_id=? ORDER BY si.id""",
            (sale["id"],),
        ).fetchall()
        item_data_rows = []
        for item in items:
            item_keys = getattr(item, "keys", None)
            if callable(item_keys):
                item_keys = item_keys()
            item_data_rows.append({key: item[key] for key in set(item_keys or ())})
        items = item_data_rows
    # Compatibilidade com pedidos antigos: antes da retirada parcial, os
    # detalhes em sale_item_deliveries não eram gravados. Um pedido com
    # delivered_at preenchido, mas sem nenhum detalhe, foi integralmente
    # entregue e não deve exibir itens pendentes.
    if sale.get("delivered_at") and items and not any(int(item.get("delivered_quantity") or 0) > 0 for item in items):
        items = [dict(item, delivered_quantity=item.get("quantity")) for item in items]
    item_data = [{
        "id": item["id"], "name": item["name"], "quantity": int(item["quantity"] or 0),
        "delivered_quantity": int(item["delivered_quantity"] or 0),
        "pending_quantity": max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0)),
    } for item in items]
    delivered_quantity = sum(item["delivered_quantity"] for item in item_data)
    pending_quantity = sum(item["pending_quantity"] for item in item_data)
    reservation = db.execute(
        """SELECT amount_cents FROM bar_credit_reservations
           WHERE sale_id=? AND status='reserved'
             AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)""",
        (sale["id"],),
    ).fetchone()
    credit_reserved_cents = int(reservation["amount_cents"] or 0) if reservation else 0
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
        "credit_reserved_cents": credit_reserved_cents,
        "cash_due_cents": max(0, int(sale.get("total_cents", 0) or 0) - credit_reserved_cents),
        "payment_method": sale.get("payment_method") or "",
        "payment_status": sale.get("payment_status") or "",
        "paid": bool(sale.get("paid")),
        "waiting_cash": sale.get("payment_status") == "pending_cash" and not sale.get("paid"),
        "can_convert_change_to_credit": bool(sale.get("player_id")),
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

    # Batch-fetch sale_items for all sales to avoid N+1 queries
    all_sale_ids = [int(s['id']) for s in list(pending) + list(delivered) + list(canceled)]
    items_for_sales = {}
    if all_sale_ids:
        placeholders = ",".join("?" for _ in all_sale_ids)
        items_rows = db.execute(
            f"""SELECT si.id AS sale_item_id, si.sale_id, si.quantity, p.id AS product_id, p.name,
                         COALESCE(SUM(sid.quantity), 0) AS delivered_quantity
                  FROM sale_items si
                  JOIN products p ON p.id=si.product_id
                  LEFT JOIN sale_item_deliveries sid ON sid.sale_item_id=si.id
                  WHERE si.sale_id IN ({placeholders})
                  GROUP BY si.id, si.sale_id, si.quantity, p.id, p.name
                  ORDER BY si.sale_id, si.id""",
            tuple(all_sale_ids),
        ).fetchall()
        for row in items_rows:
            sale_id = int(row['sale_id'])
            items_for_sales.setdefault(sale_id, []).append({
                "id": row["sale_item_id"],
                "quantity": row["quantity"],
                "name": row["name"],
                "delivered_quantity": int(row["delivered_quantity"] or 0),
            })

    return jsonify(
        pending=[delivery_order_data(db, sale, items_for_sales) for sale in pending],
        delivered=[delivery_order_data(db, sale, items_for_sales) for sale in delivered],
        canceled=[delivery_order_data(db, sale, items_for_sales) for sale in canceled],
        payment_method=payment_method,
    )

@bp.post("/orders/<int:sale_id>/confirm-payment")
@roles_allowed("manager", "staff")
def confirm_cash_payment(sale_id):
    db = get_db()
    sale = db.execute(
        "SELECT id,player_id,payment_method,total_cents,paid,payment_status FROM sales WHERE id=?",
        (sale_id,),
    ).fetchone()
    if not sale or sale["payment_method"] != "Dinheiro":
        return jsonify(error="Pedido em dinheiro não encontrado."), 404
    if sale["paid"] and sale["payment_status"] == "approved":
        return jsonify(ok=True, sale_id=sale_id, already_paid=True)
    if sale["paid"] or sale["payment_status"] != "pending_cash":
        return jsonify(error="O pedido não está aguardando pagamento em dinheiro."), 409
    payload = request.get_json(silent=True) or {}
    try:
        amount_received_cents = int(payload.get("amount_received_cents"))
    except (TypeError, ValueError):
        return jsonify(error="Informe o valor recebido em dinheiro."), 400
    reservation = db.execute(
        "SELECT amount_cents,status FROM bar_credit_reservations WHERE sale_id=?",
        (sale_id,),
    ).fetchone()
    reserved_cents = int(reservation["amount_cents"] or 0) if reservation and reservation["status"] == "reserved" else 0
    cash_due_cents = max(0, int(sale["total_cents"] or 0) - reserved_cents)
    if amount_received_cents < cash_due_cents:
        return jsonify(error="O valor recebido não pode ser menor que o restante em dinheiro."), 400
    change_cents = amount_received_cents - cash_due_cents
    convert_change = payload.get("convert_change_to_credit") is True
    if convert_change and change_cents > 0:
        player = db.execute("SELECT id FROM players WHERE id=?", (sale["player_id"],)).fetchone()
        if not player:
            return jsonify(error="Este pedido não possui peladeiro para receber créditos."), 400
    try:
        with db:
            updated = db.execute(
                """UPDATE sales
                   SET paid=1,payment_status='approved',paid_at=COALESCE(paid_at,CURRENT_TIMESTAMP)
                   WHERE id=? AND payment_method='Dinheiro' AND paid=0 AND payment_status='pending_cash'""",
                (sale_id,),
            )
            if updated.rowcount != 1:
                latest = db.execute(
                    "SELECT paid,payment_status FROM sales WHERE id=?", (sale_id,)
                ).fetchone()
                if latest and latest["paid"] and latest["payment_status"] == "approved":
                    return jsonify(ok=True, sale_id=sale_id, already_paid=True)
                return jsonify(error="O estado do pagamento mudou. Atualize a fila."), 409
            if reserved_cents:
                consume_reservation(db, sale_id, g.user["id"])
            credited = False
            balance_cents = None
            if convert_change and change_cents > 0:
                balance_cents, credited = credit_cash_change(
                    db, sale["player_id"], change_cents, sale_id, g.user["id"]
                )
    except Exception as exc:
        current_app.logger.error("Erro ao confirmar pagamento em dinheiro do pedido %s: %s", sale_id, exc)
        return jsonify(error="Não foi possível confirmar o pagamento."), 500
    return jsonify(
        ok=True, sale_id=sale_id, already_paid=False,
        change_cents=change_cents, credited=credited, balance_cents=balance_cents,
        credit_consumed_cents=reserved_cents, cash_due_cents=cash_due_cents,
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
    if sale["payment_method"] == "Dinheiro" and (
        not sale["paid"] or sale["payment_status"] != "approved"
    ):
        return jsonify(error="Confirme o pagamento em dinheiro antes da entrega."), 409
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
    delivered_items = [{"name": item["name"], "quantity": deliver_plan[item["id"]]} for item in item_rows if item["id"] in deliver_plan]
    remaining_items = [
        {
           "name": item["name"],
           "quantity": max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0) - deliver_plan.get(item["id"], 0)),
        }
        for item in item_rows
        if max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0) - deliver_plan.get(item["id"], 0)) > 0
    ]
    try:
        with db:
           delivery_operation = db.execute(
               "INSERT INTO sale_delivery_operations(sale_id,delivered_by,delivered_at) VALUES(?,?,CURRENT_TIMESTAMP)",
               (sale_id, g.user["id"]),
           )
           delivery_operation_id = delivery_operation.lastrowid
           for item_id, quantity in deliver_plan.items():
               db.execute(
                   "INSERT INTO sale_item_deliveries(delivery_operation_id,sale_item_id,quantity,delivered_by,delivered_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
                   (delivery_operation_id, item_id, quantity, g.user["id"]),
               )
           delivered_totals = db.execute(
               """SELECT si.quantity,COALESCE(SUM(sid.quantity),0) delivered_quantity
                   FROM sale_items si LEFT JOIN sale_item_deliveries sid ON sid.sale_item_id=si.id
                   WHERE si.sale_id=? GROUP BY si.id,si.quantity""", (sale_id,)
           ).fetchall()
           fully_delivered = all(int(item["delivered_quantity"] or 0) >= int(item["quantity"] or 0) for item in delivered_totals)
           if fully_delivered:
               db.execute("UPDATE sales SET delivered_at=CURRENT_TIMESTAMP,delivered_by=? WHERE id=?", (g.user["id"], sale_id))
           if sale["player_id"]:
               delivered_total = sum(item["quantity"] for item in delivered_items)
               delivery_time_row = db.execute(
                   "SELECT delivered_at FROM sale_delivery_operations WHERE id=?",
                   (delivery_operation_id,),
               ).fetchone()
               delivery_time = delivery_time_row["delivered_at"] if delivery_time_row else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
               delivered_at = brdate(delivery_time)
               payload = {
                   "delivery_push": {
                       "player_id": sale["player_id"],
                       "kind": "pedido_retirada",
                       "period": f"{sale_id}-{delivery_operation_id}-{delivery_time}",
                       "title": "Retirada confirmada",
                       "body": f"{('Pedido totalmente entregue' if not remaining_items else 'Retirada parcial registrada')}. {delivered_total} item(ns) retirado(s) em {delivered_at}." + (f" Restam {sum(item['quantity'] for item in remaining_items)} item(ns)." if remaining_items else ""),
                       "url": url_for("auth.my_purchases"),
                   },
                   "delivery_update_email": {
                       "sale_id": sale_id,
                       "delivered_items": delivered_items,
                       "remaining_items": remaining_items,
                   },
                   "purchase_receipt_email": {"sale_id": sale_id},
               }
               from src.services.notification_outbox import enqueue_delivery_events
               enqueue_delivery_events(db, sale_id, delivery_operation_id, payload)
    except Exception as exc:
        current_app.logger.error("Erro ao registrar retirada parcial do pedido %s: %s", sale_id, exc)
        return jsonify(error="Não foi possível registrar a retirada."), 500
    return jsonify(ok=True, sale_id=sale_id, partial=bool(remaining_items), remaining_items=remaining_items, receipt_status="queued")

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
            reservation = db.execute(
                "SELECT status FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
            ).fetchone()
            if reservation and reservation["status"] == "reserved":
                release_reservation(db, sale_id)
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
    if not current_app.config.get("EXTERNAL_PAYMENTS_ENABLED", True):
        return jsonify(error="Pagamento Pix indisponível na homologação."), 403
    body = request.get_json(silent=True) or {}
    sports_mode = body.get("department") == "sports" or any(
            item.get("department") == "sports" or item.get("variant_id")
            for item in body.get("items") or [])
    access_token, _ = mercadopago_config()
    if not access_token:
        return jsonify(error="A integração com Mercado Pago ainda não foi configurada."), 503

    try:
        event_id = int(body.get("event_id") or 0) or None
        guest_name = str(body.get("guest_name") or "").strip()
        player_id = int(body.get("player_id")) if body.get("player_id") else None
        if sports_mode and event_id:
            raise ValueError("Material Esportivo não está disponível para Convidado / Evento.")
        if event_id and not guest_name:
            raise ValueError("Informe o nome do convidado.")
        if not event_id and not player_id:
            raise ValueError("Selecione o peladeiro ou o evento.")
        requested = {}
        sports_requested = []
        for item in body.get("items") or []:
            product_id = int(item.get("product_id"))
            quantity = int(item.get("quantity"))
            if quantity > 0:
                if sports_mode:
                    sports_requested.append({
                        "product_id": product_id,
                        "variant_id": int(item.get("variant_id")),
                        "quantity": quantity,
                        "custom_name": _clean_sports_text(item.get("custom_name"), 40, "Nome personalizado"),
                        "custom_number": _clean_sports_text(item.get("custom_number"), 10, "Número"),
                        "order_mode": str(item.get("order_mode") or ""),
                    })
                else:
                    requested[product_id] = requested.get(product_id, 0) + quantity
        if not requested and not sports_requested:
            raise ValueError("Escolha ao menos um produto.")
    except (TypeError, ValueError):
        return jsonify(error="Selecione o peladeiro e produtos válidos."), 400

    db = get_db()
    event = db.execute("SELECT id,name FROM bar_events WHERE id=? AND status='open'", (event_id,)).fetchone() if event_id else None
    player = db.execute("SELECT id,email FROM players WHERE id=? AND active=1", (player_id,)).fetchone() if player_id else None
    products_by_id = {}
    sports_by_variant = {}
    if sports_mode:
        variant_ids = [item["variant_id"] for item in sports_requested]
        placeholders = ",".join("?" for _ in variant_ids)
        rows = db.execute(
            f"""SELECT v.id variant_id,v.product_id,v.size,v.stock,v.active variant_active,
                       p.name,p.price_cents,p.cost_cents,p.active product_active,p.category,
                       c.allow_custom_name,c.allow_custom_number,c.allow_backorder
                FROM sports_product_variants v JOIN products p ON p.id=v.product_id
                JOIN sports_product_config c ON c.product_id=p.id
                WHERE v.id IN ({placeholders})""",
            tuple(variant_ids),
        ).fetchall()
        sports_by_variant = {row["variant_id"]: row for row in rows}
    else:
        placeholders = ",".join("?" for _ in requested)
        products = db.execute(
            f"SELECT id,name,price_cents,cost_cents,stock FROM products WHERE active=1 AND id IN ({placeholders})",
            tuple(requested),
        ).fetchall()
        products_by_id = {product["id"]: product for product in products}
    invalid_products = (
        len(sports_by_variant) != len(set(item["variant_id"] for item in sports_requested))
        if sports_mode else len(products_by_id) != len(requested)
    )
    if (event_id and not event) or (not event_id and not player) or invalid_products:
        return jsonify(error="Peladeiro, evento ou produto inválido."), 400
    payer_email = str(player["email"] or "").strip().lower() if player else str(current_app.config.get("GMAIL_SMTP_USER") or "").strip().lower()
    if "@" not in payer_email:
        return jsonify(error="Configure um e-mail válido para gerar o Pix do evento." if event_id else "Cadastre um e-mail válido para o peladeiro antes de gerar o Pix."), 400
    if sports_mode:
        for item in sports_requested:
            row = sports_by_variant[item["variant_id"]]
            if (row["product_id"] != item["product_id"] or not row["product_active"] or
                    not row["variant_active"] or row["category"] != SPORTS_MATERIAL_CATEGORY):
                return jsonify(error="Produto ou tamanho esportivo inválido."), 400
            if item["custom_name"] and not row["allow_custom_name"]:
                return jsonify(error="Este produto não permite nome personalizado."), 400
            if item["custom_number"] and not row["allow_custom_number"]:
                return jsonify(error="Este produto não permite número personalizado."), 400
            if item["order_mode"] not in {"ready", "backorder"}:
                return jsonify(error="Escolha pronta entrega ou encomenda."), 400
            if item["order_mode"] == "backorder":
                return jsonify(error="Encomendas são solicitadas sem pagamento. Use Solicitar encomenda."), 409
            if item["order_mode"] == "backorder" and not row["allow_backorder"]:
                return jsonify(error="Este produto não permite encomenda."), 400
            if item["order_mode"] == "ready" and row["stock"] < item["quantity"]:
                return jsonify(error=f"Estoque insuficiente para {row['name']} — {row['size']}."), 409
    else:
        for product_id, quantity in requested.items():
            if products_by_id[product_id]["stock"] < quantity:
                return jsonify(error=f"Estoque insuficiente de {products_by_id[product_id]['name']}."), 409

    total_cents = (
        sum(sports_by_variant[item["variant_id"]]["price_cents"] * item["quantity"] for item in sports_requested)
        if sports_mode else
        sum(products_by_id[product_id]["price_cents"] * quantity for product_id, quantity in requested.items())
    )
    use_bar_credit = body.get("use_bar_credit") is True and bool(player_id) and not sports_mode
    credit_amount = min(available_credit_balance(db, player_id), total_cents) if use_bar_credit else 0
    external_cents = total_cents - credit_amount
    full_credit = credit_amount == total_cents and credit_amount > 0
    external_reference = f"evento_{uuid.uuid4().hex}" if event_id else f"pelada_{uuid.uuid4().hex}"
    idempotency_key = str(uuid.uuid4())
    try:
        with db:
            sale_cursor = db.execute(
                """INSERT INTO sales(player_id,event_id,guest_name,payment_method,total_cents,paid,payment_status,external_reference,idempotency_key,notes)
                   VALUES(?,?,?,? ,?,'0','creating',?,?,?)""",
                (player_id, event_id, guest_name, "Créditos" if full_credit else "Pix", total_cents, external_reference, idempotency_key, str(body.get("notes") or "").strip()),
            )
            sale_id = sale_cursor.lastrowid
            if credit_amount and not full_credit:
                reserve_credit(db, player_id, sale_id, credit_amount)
            if sports_mode:
                for item in sports_requested:
                    product = sports_by_variant[item["variant_id"]]
                    if item["order_mode"] == "ready":
                        updated = db.execute(
                            """UPDATE sports_product_variants
                               SET stock=stock-?,updated_at=CURRENT_TIMESTAMP
                               WHERE id=? AND product_id=? AND active AND stock>=?""",
                            (item["quantity"], item["variant_id"], item["product_id"], item["quantity"]),
                        )
                        if updated.rowcount != 1:
                            raise ValueError(f"O estoque de {product['name']} mudou. Tente novamente.")
                    sale_item = db.execute(
                        """INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents)
                           VALUES(?,?,?,?,?)""",
                        (sale_id, item["product_id"], item["quantity"],
                         product["price_cents"], product["cost_cents"]),
                    )
                    db.execute(
                        """INSERT INTO sports_sale_item_details
                           (sale_item_id,variant_id,variant_size,custom_name,custom_number,
                            order_mode,fulfillment_status) VALUES(?,?,?,?,?,?,?)""",
                        (sale_item.lastrowid, item["variant_id"], product["size"],
                         item["custom_name"], item["custom_number"], item["order_mode"],
                         "reserved" if item["order_mode"] == "ready" else "requested"),
                    )
                    if item["order_mode"] == "ready":
                        db.execute(
                            """INSERT INTO sports_stock_reservations
                               (sale_item_id,variant_id,quantity,status) VALUES(?,?,?,'reserved')""",
                            (sale_item.lastrowid, item["variant_id"], item["quantity"]),
                        )
            else:
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
            if full_credit:
                consume_credit(
                    db, player_id, total_cents, sale_id,
                    g.user["id"] if g.user else None,
                )
                db.execute(
                    """UPDATE sales SET paid=1,payment_status='approved',
                       paid_at=CURRENT_TIMESTAMP,ready_for_delivery=1 WHERE id=?""",
                    (sale_id,),
                )
    except ValueError as exc:
        return jsonify(error=str(exc)), 409

    if full_credit:
        if not sports_mode:
            notify_low_stock(db, requested.keys())
        return jsonify(
            sale_id=sale_id,
            amount=money(0),
            status="approved",
            paid=True,
            status_url=url_for("sales.receipt", sale_id=sale_id),
        ), 201

    try:
        order = create_pix_order(access_token, external_reference, external_cents, idempotency_key, payer_email)
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
        if not sports_mode:
            notify_low_stock(db, requested.keys())
        encoded_image = generate_qrcode_base64(qr_data)
        return jsonify(
            sale_id=sale_id,
            order_id=order_id,
            payload=qr_data,
            image=f"data:image/png;base64,{encoded_image}",
            amount=money(external_cents),
            status="pending",
            status_url=url_for("sales.mercadopago_order_status", sale_id=sale_id),
        ), 201
    except Exception as exc:
        current_app.logger.error(f"Erro ao criar order Mercado Pago: {exc}")
        with db:
            sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            if sale and sale["payment_status"] == "creating":
                reservation = db.execute(
                    "SELECT status FROM bar_credit_reservations WHERE sale_id=?", (sale_id,)
                ).fetchone()
                if reservation and reservation["status"] == "reserved":
                    release_reservation(db, sale_id)
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
