from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify, current_app
from itsdangerous import BadData, URLSafeTimedSerializer
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import money
from src.services.pix import pix_payload, generate_qrcode_base64

bp = Blueprint("sales", __name__)
PIX_TOKEN_MAX_AGE = 60 * 60

def pix_access_token(user):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pix-qrcode")
    return serializer.dumps({"user_id": user["id"], "role": user["role"]})

def validate_pix_access_token(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pix-qrcode")
    data = serializer.loads(token, max_age=PIX_TOKEN_MAX_AGE)
    return data.get("role") in ("manager", "staff", "client")

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
            if g.user["role"] == "client" and method not in ("Pix", "Dinheiro"):
                raise ValueError("Clientes podem registrar pagamentos somente em Pix ou Dinheiro.")
            
            paid = 1
            with db:
                cur = db.execute(
                    "INSERT INTO sales(player_id,payment_method,total_cents,paid,notes) VALUES(?,?,?,?,?)",
                    (player_id, method, total, paid, request.form.get("notes", "").strip())
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
            
            flash(f"Venda #{cur.lastrowid} registrada: {money(total)}.", "success")
            return redirect(url_for("sales.sale"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao processar venda: {exc}")
            flash("Erro interno ao processar a venda. Tente novamente.", "danger")

    player_rows = db.execute("SELECT * FROM players WHERE active=1 ORDER BY name").fetchall()
    product_rows = db.execute("SELECT * FROM products WHERE active=1 AND stock>0 ORDER BY category, name").fetchall()
    return render_template(
        "sale.html",
        players=player_rows,
        products=product_rows,
        pix_token=pix_access_token(g.user),
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
    day = request.args.get("day", date.today().isoformat())
    rows = db.execute(
        """SELECT s.*, p.name player_name FROM sales s JOIN players p ON p.id=s.player_id
        WHERE date(s.created_at)=? AND s.payment_method='Pix' ORDER BY s.id DESC""",
        (day,)
    ).fetchall()
    total = sum(r["total_cents"] for r in rows)
    return render_template("pix.html", rows=rows, total=total, day=day)

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
