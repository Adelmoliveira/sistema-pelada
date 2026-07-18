from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, send_file
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import cents
from src.services.cash_register import create_movement, get_session
from src.services.stock_report_pdf import build_stock_report_pdf, stock_report_data, build_low_stock_pdf, low_stock_report_data
from src.services.stock_alerts import notify_low_stock
from src.utils import local_today

bp = Blueprint("products", __name__)

@bp.route("/products", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def products():
    db = get_db()
    if request.method == "POST":
        try:
            units_per_case = int(request.form.get("units_per_case") or 0)
            loose_units = int(request.form.get("stock") or 0)
            cases = int(request.form.get("initial_cases") or 0)
            if min(units_per_case, loose_units, cases) < 0:
                raise ValueError("As quantidades não podem ser negativas.")
            if cases and not units_per_case:
                raise ValueError("Informe quantas unidades vêm em cada caixa.")
            
            initial_stock = loose_units + cases * units_per_case
            created = db.execute(
                """INSERT INTO products(name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,supplier_email)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    request.form["name"].strip(),
                    request.form["category"],
                    request.form.get("package_type", ""),
                    units_per_case,
                    cents(request.form["price"]),
                    cents(request.form.get("cost", "0")),
                    initial_stock,
                    int(request.form.get("min_stock", 5)),
                    request.form.get("supplier_email", "").strip().lower(),
                )
            )
            db.commit()
            notify_low_stock(db, [created.lastrowid])
            flash("Produto cadastrado.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao cadastrar produto: {exc}")
            if "unique" in str(exc).lower():
                flash("Não foi possível cadastrar: Já existe outro produto com esse nome.", "danger")
            else:
                flash("Não foi possível cadastrar devido a um erro interno.", "danger")
        return redirect(url_for("products.products"))

    items = db.execute("SELECT * FROM products ORDER BY active DESC, category, name").fetchall()
    return render_template("products.html", products=items)

@bp.post("/products/<int:product_id>/toggle")
@roles_allowed("manager", "staff")
def toggle_product(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        flash("Produto não encontrado.", "warning")
    else:
        try:
            db.execute("UPDATE products SET active=1-active WHERE id=?", (product_id,))
            db.commit()
            flash("Produto excluído dos cadastros ativos; o histórico foi preservado." if product["active"]
                  else "Produto restaurado.", "success")
        except Exception as exc:
            current_app.logger.error(f"Erro ao alternar atividade do produto {product_id}: {exc}")
            flash("Erro interno ao atualizar status do produto.", "danger")
    return redirect(url_for("products.products"))

@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def edit_product(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("products.products"))
    
    if request.method == "POST":
        try:
            units_per_case = int(request.form.get("units_per_case") or 0)
            min_stock = int(request.form.get("min_stock") or 0)
            new_stock = int(request.form.get("stock") or 0)
            if units_per_case < 0 or min_stock < 0 or new_stock < 0:
                raise ValueError("As quantidades não podem ser negativas.")
            
            stock_changed = new_stock != product["stock"]
            reason = request.form.get("stock_reason", "").strip()
            if stock_changed and not reason:
                raise ValueError("Informe o motivo do ajuste de estoque.")

            db.execute(
                """UPDATE products SET name=?,category=?,package_type=?,units_per_case=?,
                price_cents=?,cost_cents=?,min_stock=?,stock=?,supplier_email=? WHERE id=?""",
                (
                    request.form["name"].strip(),
                    request.form["category"],
                    request.form.get("package_type", ""),
                    units_per_case,
                    cents(request.form["price"]),
                    cents(request.form.get("cost", "0")),
                    min_stock,
                    new_stock,
                    request.form.get("supplier_email", "").strip().lower(),
                    product_id
                )
            )
            if stock_changed:
                db.execute("""INSERT INTO stock_adjustments
                    (product_id,user_id,previous_stock,new_stock,difference,reason)
                    VALUES(?,?,?,?,?,?)""", (product_id, g.user["id"], product["stock"], new_stock,
                    new_stock - product["stock"], reason))
            db.commit()
            notify_low_stock(db, [product_id])
            flash("Produto atualizado.", "success")
            return redirect(url_for("products.products"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao editar produto {product_id}: {exc}")
            if "unique" in str(exc).lower():
                flash("Já existe outro produto com esse nome.", "danger")
            else:
                flash("Erro interno ao atualizar produto.", "danger")
        product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    return render_template("edit_product.html", product=product)

@bp.route("/stock", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def stock():
    db = get_db()
    if request.method == "POST":
        try:
            pid = int(request.form["product_id"])
            product = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            if not product:
                raise ValueError("Produto inválido.")
            
            loose_units = int(request.form.get("quantity") or 0)
            cases = int(request.form.get("cases") or 0)
            if min(loose_units, cases) < 0:
                raise ValueError("As quantidades não podem ser negativas.")
            if cases and not product["units_per_case"]:
                raise ValueError("Este produto não possui unidades por caixa cadastradas.")
            
            qty = loose_units + cases * product["units_per_case"]
            if qty <= 0:
                raise ValueError("Informe unidades avulsas ou quantidade de caixas.")
            
            cost = cents(request.form.get("unit_cost", "0"))
            payment_account = request.form.get("payment_account", "unpaid")
            if payment_account not in {"unpaid", "cash", "bank"}:
                raise ValueError("Forma de pagamento da compra inválida.")
            cash_session = None
            if payment_account != "unpaid":
                if cost <= 0:
                    raise ValueError("Informe o custo unitário para registrar o pagamento no caixa.")
                cash_session = get_session(db)
                if not cash_session or cash_session["status"] != "open":
                    raise ValueError("Abra o caixa de hoje antes de registrar uma compra paga.")
            with db:
                restock = db.execute(
                    "INSERT INTO restocks(product_id,quantity,unit_cost_cents,notes) VALUES(?,?,?,?)",
                    (pid, qty, cost, (f"{cases} caixa(s). " if cases else "") + request.form.get("notes", "").strip())
                )
                db.execute(
                    "UPDATE products SET stock=stock+?, cost_cents=CASE WHEN ?>0 THEN ? ELSE cost_cents END WHERE id=?",
                    (qty, cost, cost, pid)
                )
                if payment_account != "unpaid":
                    create_movement(
                        db,
                        cash_session["id"],
                        payment_account,
                        "out",
                        "purchase",
                        qty * cost,
                        f"Compra de estoque: {product['name']} ({qty} un.)",
                        g.user["id"],
                        source="restock",
                        source_id=restock.lastrowid,
                    )
            flash("Reposição registrada e estoque atualizado.", "success")
            notify_low_stock(db, [pid])
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro no registro de reposição: {exc}")
            flash("Erro interno ao registrar reposição de estoque.", "danger")
        return redirect(url_for("products.stock"))

    product_rows = db.execute("SELECT * FROM products WHERE active=1 ORDER BY stock, name").fetchall()
    history = db.execute(
        """SELECT r.*,p.name product_name,m.account payment_account,m.amount_cents paid_amount_cents,
        c.corrected_quantity,c.corrected_unit_cost_cents,
        c.reason correction_reason,c.created_at correction_created_at,u.name correction_user_name
        FROM restocks r JOIN products p ON p.id=r.product_id
        LEFT JOIN cash_movements m ON m.source='restock' AND m.source_id=r.id
        LEFT JOIN restock_corrections c ON c.id=(
            SELECT MAX(c2.id) FROM restock_corrections c2 WHERE c2.restock_id=r.id
        )
        LEFT JOIN users u ON u.id=c.created_by
        ORDER BY r.id DESC LIMIT 30"""
    ).fetchall()
    adjustments = db.execute(
        """SELECT a.*,p.name product_name,u.name user_name FROM stock_adjustments a
        JOIN products p ON p.id=a.product_id LEFT JOIN users u ON u.id=a.user_id
        ORDER BY a.id DESC LIMIT 30"""
    ).fetchall()
    return render_template("stock.html", products=product_rows, history=history, adjustments=adjustments,
                           report_start=request.args.get("start", ""), report_end=request.args.get("end", ""))


@bp.get("/stock/report.pdf")
@roles_allowed("manager", "staff")
def stock_report():
    def parse_date(value, label):
        value = (value or "").strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"Informe uma data válida para {label}.")

    try:
        start_date = parse_date(request.args.get("start"), "o início do período")
        end_date = parse_date(request.args.get("end"), "o fim do período")
        if start_date and end_date and start_date > end_date:
            raise ValueError("A data inicial não pode ser posterior à data final.")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("products.stock"))
    report = build_stock_report_pdf(
        stock_report_data(get_db(), start_date.isoformat() if start_date else "", end_date.isoformat() if end_date else ""),
        start_date, end_date, local_today(),
    )
    return send_file(
        report, mimetype="application/pdf", as_attachment=True,
        download_name=f"relatorio-estoque-{local_today().isoformat()}.pdf",
    )


@bp.get("/stock/low-report.pdf")
@roles_allowed("manager", "staff")
def low_stock_report():
    report = build_low_stock_pdf(low_stock_report_data(get_db()), local_today())
    return send_file(
        report, mimetype="application/pdf", as_attachment=False,
        download_name=f"estoque-baixo-{local_today().isoformat()}.pdf",
    )


@bp.route("/stock/restocks/<int:restock_id>/correct", methods=["GET", "POST"])
@roles_allowed("manager")
def correct_restock(restock_id):
    db = get_db()
    restock = db.execute(
        """SELECT r.*,p.name product_name,p.stock current_stock,
        c.corrected_quantity,c.corrected_unit_cost_cents
        FROM restocks r JOIN products p ON p.id=r.product_id
        LEFT JOIN restock_corrections c ON c.id=(
            SELECT MAX(c2.id) FROM restock_corrections c2 WHERE c2.restock_id=r.id
        ) WHERE r.id=?""",
        (restock_id,),
    ).fetchone()
    if not restock:
        flash("Reposição não encontrada.", "warning")
        return redirect(url_for("products.stock"))

    effective_quantity = restock["corrected_quantity"] if restock["corrected_quantity"] is not None else restock["quantity"]
    effective_cost = restock["corrected_unit_cost_cents"] if restock["corrected_unit_cost_cents"] is not None else restock["unit_cost_cents"]
    if request.method == "POST":
        try:
            corrected_quantity = int(request.form.get("quantity", ""))
            corrected_cost = cents(request.form.get("unit_cost", "0"))
            reason = request.form.get("reason", "").strip()
            if corrected_quantity < 0 or corrected_cost < 0:
                raise ValueError("Quantidade e custo não podem ser negativos.")
            if len(reason) < 5:
                raise ValueError("Informe um motivo com pelo menos 5 caracteres.")
            new_stock = restock["current_stock"] + corrected_quantity - effective_quantity
            if new_stock < 0:
                raise ValueError("Não é possível reduzir essa quantidade porque parte do estoque já foi utilizada.")
            latest_restock = db.execute(
                "SELECT MAX(id) latest_id FROM restocks WHERE product_id=?",
                (restock["product_id"],),
            ).fetchone()["latest_id"]
            with db:
                db.execute(
                    """INSERT INTO restock_corrections
                    (restock_id,previous_quantity,corrected_quantity,previous_unit_cost_cents,
                     corrected_unit_cost_cents,reason,created_by)
                    VALUES(?,?,?,?,?,?,?)""",
                    (restock_id, effective_quantity, corrected_quantity, effective_cost,
                     corrected_cost, reason, g.user["id"]),
                )
                db.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, restock["product_id"]))
                if latest_restock == restock_id:
                    db.execute(
                        "UPDATE products SET cost_cents=? WHERE id=?",
                        (corrected_cost, restock["product_id"]),
                    )
                db.execute(
                    """INSERT INTO stock_adjustments
                    (product_id,user_id,previous_stock,new_stock,difference,reason)
                    VALUES(?,?,?,?,?,?)""",
                    (restock["product_id"], g.user["id"], restock["current_stock"], new_stock,
                     corrected_quantity - effective_quantity,
                     f"Correção da reposição #{restock_id}: {reason}"),
                )
            flash(f"Reposição #{restock_id} corrigida com histórico preservado.", "success")
            return redirect(url_for("products.stock"), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao corrigir reposição {restock_id}: {exc}")
            flash("Erro interno ao corrigir a reposição.", "danger")
    return render_template(
        "correct_restock.html", restock=restock,
        effective_quantity=effective_quantity, effective_cost=effective_cost,
    )
