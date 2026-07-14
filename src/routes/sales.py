from datetime import date
import re
import uuid

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.mercadopago import MercadoPagoError, create_pix_payment, get_payment, validate_webhook_signature
from src.services.pix import generate_qrcode_base64, pix_payload
from src.utils import money

bp = Blueprint("sales", __name__)


def _requested_items(form):
    requested = {}
    for raw_id, raw_qty in zip(form.getlist("product_id"), form.getlist("quantity")):
        qty = int(raw_qty or 0)
        if qty > 0:
            product_id = int(raw_id)
            requested[product_id] = requested.get(product_id, 0) + qty
    if not requested:
        raise ValueError("Escolha ao menos um produto.")
    return requested


def _load_order(db, requested):
    placeholders = ",".join("?" for _ in requested)
    products = {row["id"]: row for row in db.execute(
        f"SELECT * FROM products WHERE active=1 AND id IN ({placeholders})", tuple(requested)
    )}
    if len(products) != len(requested):
        raise ValueError("Produto inválido ou inativo.")
    for product_id, quantity in requested.items():
        if products[product_id]["stock"] < quantity:
            raise ValueError(f"Estoque insuficiente de {products[product_id]['name']}.")
    return products, sum(products[pid]["price_cents"] * qty for pid, qty in requested.items())


def _release_pix_stock(db, pix_id):
    intent = db.execute("SELECT stock_reserved FROM pix_payments WHERE id=?", (pix_id,)).fetchone()
    if not intent or not intent["stock_reserved"]:
        return
    for item in db.execute("SELECT product_id,quantity FROM pix_payment_items WHERE pix_payment_id=?", (pix_id,)):
        db.execute("UPDATE products SET stock=stock+? WHERE id=?", (item["quantity"], item["product_id"]))
    db.execute("UPDATE pix_payments SET stock_reserved=0 WHERE id=?", (pix_id,))


def _reconcile_pix_payment(db, payment):
    payment_id = str(payment.get("id", ""))
    intent = db.execute(
        "SELECT * FROM pix_payments WHERE mp_payment_id=? OR reference=?",
        (payment_id, payment.get("external_reference", "")),
    ).fetchone()
    if not intent:
        return None
    if payment.get("payment_method_id") != "pix":
        raise ValueError("O pagamento recebido não é Pix.")
    if round(float(payment.get("transaction_amount", 0)) * 100) != intent["amount_cents"]:
        raise ValueError("O valor confirmado pelo Mercado Pago diverge da cobrança.")

    status, detail = payment.get("status", "unknown"), payment.get("status_detail", "")
    if status == "approved" and not intent["sale_id"]:
        with db:
            claimed = db.execute(
                """UPDATE pix_payments SET status='processing',status_detail=?,mp_payment_id=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND sale_id IS NULL AND stock_reserved=1 AND status!='processing'""",
                (detail, payment_id, intent["id"]),
            )
            if claimed.rowcount == 1:
                sale = db.execute(
                    "INSERT INTO sales(player_id,payment_method,total_cents,paid,notes) VALUES(?,'Pix',?,1,?)",
                    (intent["player_id"], intent["amount_cents"], f"Mercado Pago #{payment_id}"),
                )
                for item in db.execute("SELECT * FROM pix_payment_items WHERE pix_payment_id=?", (intent["id"],)):
                    db.execute(
                        """INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents)
                           VALUES(?,?,?,?,?)""",
                        (sale.lastrowid, item["product_id"], item["quantity"], item["unit_price_cents"], item["unit_cost_cents"]),
                    )
                db.execute(
                    """UPDATE pix_payments SET status='approved',status_detail=?,sale_id=?,stock_reserved=0,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (detail, sale.lastrowid, intent["id"]),
                )
    elif status in ("rejected", "cancelled", "refunded", "charged_back"):
        with db:
            _release_pix_stock(db, intent["id"])
            db.execute("UPDATE pix_payments SET status=?,status_detail=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (status, detail, intent["id"]))
    elif not intent["sale_id"]:
        with db:
            db.execute(
                """UPDATE pix_payments SET status=?,status_detail=?,mp_payment_id=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""", (status, detail, payment_id, intent["id"]),
            )
    return intent["id"]


@bp.route("/sale", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "client")
def sale():
    db = get_db()
    if request.method == "POST":
        try:
            player_id = int(request.form["player_id"])
            requested = _requested_items(request.form)
            products, total = _load_order(db, requested)
            method = request.form["payment_method"]
            if g.user["role"] == "client" and method not in ("Pix", "Dinheiro"):
                raise ValueError("Clientes podem registrar pagamentos somente em Pix ou Dinheiro.")
            if method == "Pix" and current_app.config.get("MERCADOPAGO_ACCESS_TOKEN"):
                raise ValueError("Para Pix, gere a cobrança e aguarde a confirmação automática.")
            with db:
                cursor = db.execute(
                    "INSERT INTO sales(player_id,payment_method,total_cents,paid,notes) VALUES(?,?,?,?,?)",
                    (player_id, method, total, 1, request.form.get("notes", "").strip()),
                )
                for product_id, quantity in requested.items():
                    product = products[product_id]
                    db.execute(
                        "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_cents,unit_cost_cents) VALUES(?,?,?,?,?)",
                        (cursor.lastrowid, product_id, quantity, product["price_cents"], product["cost_cents"]),
                    )
                    if db.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                                  (quantity, product_id, quantity)).rowcount != 1:
                        raise ValueError("O estoque mudou durante a venda. Tente novamente.")
            flash(f"Venda #{cursor.lastrowid} registrada: {money(total)}.", "success")
            return redirect(url_for("sales.sale"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error("Erro ao processar venda: %s", exc)
            flash("Erro interno ao processar a venda. Tente novamente.", "danger")
    players = db.execute("SELECT * FROM players WHERE active=1 ORDER BY name").fetchall()
    products = db.execute("SELECT * FROM products WHERE active=1 AND stock>0 ORDER BY category,name").fetchall()
    return render_template("sale.html", players=players, products=products,
                           mercadopago_enabled=bool(current_app.config.get("MERCADOPAGO_ACCESS_TOKEN")))


@bp.post("/sales/<int:sale_id>/delete")
@roles_allowed("manager", "staff")
def delete_sale(sale_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM sales WHERE id=?", (sale_id,)).fetchone():
        flash("Venda não encontrada ou já apagada.", "warning")
        return redirect(request.referrer or url_for("finance.reports"))
    if db.execute("SELECT 1 FROM pix_payments WHERE sale_id=?", (sale_id,)).fetchone():
        flash("Vendas confirmadas automaticamente pelo Mercado Pago não podem ser apagadas.", "warning")
        return redirect(request.referrer or url_for("finance.reports"))
    try:
        with db:
            for item in db.execute("SELECT product_id,quantity FROM sale_items WHERE sale_id=?", (sale_id,)):
                db.execute("UPDATE products SET stock=stock+? WHERE id=?", (item["quantity"], item["product_id"]))
            db.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        flash(f"Venda #{sale_id} apagada e itens devolvidos ao estoque.", "success")
    except Exception as exc:
        current_app.logger.error("Erro ao deletar venda %s: %s", sale_id, exc)
        flash("Erro interno ao apagar a venda.", "danger")
    return redirect(request.referrer or url_for("finance.reports"))


@bp.route("/pix")
@roles_allowed("manager", "staff")
def pix():
    db, day = get_db(), request.args.get("day", date.today().isoformat())
    rows = db.execute(
        """SELECT s.*,p.name player_name FROM sales s JOIN players p ON p.id=s.player_id
           WHERE date(s.created_at)=? AND s.payment_method='Pix' ORDER BY s.id DESC""", (day,)
    ).fetchall()
    return render_template("pix.html", rows=rows, total=sum(row["total_cents"] for row in rows), day=day)


@bp.get("/pix/qrcode")
@roles_allowed("manager", "staff", "client")
def pix_qrcode():
    try:
        amount = int(request.args.get("amount_cents", 0))
        if amount <= 0 or amount > 100_000_000:
            raise ValueError
        payload = pix_payload(amount, current_app.config["PIX_KEY"], current_app.config["PIX_MERCHANT_NAME"], current_app.config["PIX_MERCHANT_CITY"])
        return jsonify(payload=payload, image=f"data:image/png;base64,{generate_qrcode_base64(payload)}",
                       key=current_app.config["PIX_KEY"], amount=money(amount))
    except ValueError:
        return jsonify(error="Selecione produtos para gerar um Pix com valor válido."), 400
    except Exception as exc:
        current_app.logger.error("Erro ao gerar QR Code de Pix: %s", exc)
        return jsonify(error="Erro interno ao gerar o QR Code de Pix."), 500


@bp.post("/pix/payment")
@roles_allowed("manager", "staff", "client")
def create_mercadopago_pix():
    token = current_app.config.get("MERCADOPAGO_ACCESS_TOKEN")
    if not token:
        return jsonify(error="A integração do Mercado Pago ainda não foi configurada."), 503
    db, pix_id = get_db(), None
    try:
        player_id = int(request.form["player_id"])
        player = db.execute("SELECT * FROM players WHERE id=? AND active=1", (player_id,)).fetchone()
        if not player:
            raise ValueError("Selecione um peladeiro válido.")
        email, cpf = (player["email"] or "").strip(), re.sub(r"\D", "", player["cpf"] or "")
        if "@" not in email:
            raise ValueError("Cadastre um e-mail válido para este peladeiro antes de gerar o Pix.")
        if len(cpf) != 11:
            raise ValueError("Cadastre um CPF válido para este peladeiro antes de gerar o Pix.")
        requested = _requested_items(request.form)
        products, total = _load_order(db, requested)
        reference, idem = f"bar-pelada-{uuid.uuid4()}", str(uuid.uuid4())
        with db:
            pix_id = db.execute(
                "INSERT INTO pix_payments(reference,idempotency_key,player_id,amount_cents,status) VALUES(?,?,?,?,'creating')",
                (reference, idem, player_id, total),
            ).lastrowid
            for product_id, quantity in requested.items():
                product = products[product_id]
                db.execute(
                    """INSERT INTO pix_payment_items(pix_payment_id,product_id,quantity,unit_price_cents,unit_cost_cents)
                       VALUES(?,?,?,?,?)""", (pix_id, product_id, quantity, product["price_cents"], product["cost_cents"]),
                )
                if db.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                              (quantity, product_id, quantity)).rowcount != 1:
                    raise ValueError("O estoque mudou. Atualize a página e tente novamente.")
        parts = (player["name"] or "Peladeiro").strip().split(maxsplit=1)
        payer = {"email": email, "first_name": parts[0], "last_name": parts[1] if len(parts) > 1 else "Peladeiro",
                 "identification": {"type": "CPF", "number": cpf}}
        names = ", ".join(f"{qty}x {products[pid]['name']}" for pid, qty in requested.items())
        base_url = current_app.config.get("APP_BASE_URL")
        payment = create_pix_payment(token, total, f"BAR PELADEIROS GPCTA - {names}"[:250], payer,
                                     reference, idem, f"{base_url}/webhooks/mercadopago" if base_url else None)
        transaction = payment.get("point_of_interaction", {}).get("transaction_data", {})
        with db:
            db.execute(
                """UPDATE pix_payments SET mp_payment_id=?,status=?,status_detail=?,qr_code=?,qr_code_base64=?,
                   ticket_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (str(payment["id"]), payment.get("status", "pending"), payment.get("status_detail", ""),
                 transaction.get("qr_code", ""), transaction.get("qr_code_base64", ""),
                 transaction.get("ticket_url", ""), pix_id),
            )
        if payment.get("status") == "approved":
            _reconcile_pix_payment(db, payment)
        return jsonify(id=pix_id, image=f"data:image/png;base64,{transaction.get('qr_code_base64', '')}",
                       payload=transaction.get("qr_code", ""), ticket_url=transaction.get("ticket_url", ""),
                       amount=money(total), status=payment.get("status", "pending"))
    except (ValueError, KeyError) as exc:
        code, message = 400, str(exc)
    except MercadoPagoError as exc:
        code, message = 502, str(exc)
        current_app.logger.warning("Erro ao criar Pix: %s", exc)
    except Exception as exc:
        code, message = 500, "Não foi possível criar a cobrança Pix."
        current_app.logger.exception("Erro inesperado ao criar Pix: %s", exc)
    if pix_id:
        try:
            with db:
                _release_pix_stock(db, pix_id)
                db.execute("UPDATE pix_payments SET status='error',status_detail=? WHERE id=?", (message, pix_id))
        except Exception:
            db.rollback()
    return jsonify(error=message), code


@bp.get("/pix/payment/<int:pix_id>/status")
@roles_allowed("manager", "staff", "client")
def mercadopago_pix_status(pix_id):
    db = get_db()
    intent = db.execute("SELECT * FROM pix_payments WHERE id=?", (pix_id,)).fetchone()
    if not intent:
        return jsonify(error="Cobrança não encontrada."), 404
    if intent["mp_payment_id"] and intent["status"] not in ("approved", "rejected", "cancelled"):
        try:
            _reconcile_pix_payment(db, get_payment(current_app.config["MERCADOPAGO_ACCESS_TOKEN"], intent["mp_payment_id"]))
        except Exception as exc:
            current_app.logger.warning("Falha ao consultar Pix %s: %s", pix_id, exc)
    current = db.execute("SELECT status,status_detail,sale_id FROM pix_payments WHERE id=?", (pix_id,)).fetchone()
    return jsonify(status=current["status"], detail=current["status_detail"], sale_id=current["sale_id"])


@bp.post("/webhooks/mercadopago")
def mercadopago_webhook():
    body = request.get_json(silent=True) or {}
    data_id = request.args.get("data.id") or (body.get("data") or {}).get("id")
    if not validate_webhook_signature(request.headers.get("x-signature"), request.headers.get("x-request-id"),
                                      data_id, current_app.config.get("MERCADOPAGO_WEBHOOK_SECRET")):
        return "Assinatura inválida", 401
    try:
        _reconcile_pix_payment(get_db(), get_payment(current_app.config["MERCADOPAGO_ACCESS_TOKEN"], data_id))
    except Exception as exc:
        current_app.logger.exception("Erro no webhook Mercado Pago: %s", exc)
        return "Erro ao processar", 500
    return "", 200
