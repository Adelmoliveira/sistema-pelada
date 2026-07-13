from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import cents

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
            db.execute(
                """INSERT INTO products(name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    request.form["name"].strip(),
                    request.form["category"],
                    request.form.get("package_type", ""),
                    units_per_case,
                    cents(request.form["price"]),
                    cents(request.form.get("cost", "0")),
                    initial_stock,
                    int(request.form.get("min_stock", 5))
                )
            )
            db.commit()
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
                price_cents=?,cost_cents=?,min_stock=?,stock=? WHERE id=?""",
                (
                    request.form["name"].strip(),
                    request.form["category"],
                    request.form.get("package_type", ""),
                    units_per_case,
                    cents(request.form["price"]),
                    cents(request.form.get("cost", "0")),
                    min_stock,
                    new_stock,
                    product_id
                )
            )
            if stock_changed:
                db.execute("""INSERT INTO stock_adjustments
                    (product_id,user_id,previous_stock,new_stock,difference,reason)
                    VALUES(?,?,?,?,?,?)""", (product_id, g.user["id"], product["stock"], new_stock,
                    new_stock - product["stock"], reason))
            db.commit()
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
            with db:
                db.execute(
                    "INSERT INTO restocks(product_id,quantity,unit_cost_cents,notes) VALUES(?,?,?,?)",
                    (pid, qty, cost, (f"{cases} caixa(s). " if cases else "") + request.form.get("notes", "").strip())
                )
                db.execute(
                    "UPDATE products SET stock=stock+?, cost_cents=CASE WHEN ?>0 THEN ? ELSE cost_cents END WHERE id=?",
                    (qty, cost, cost, pid)
                )
            flash("Reposição registrada e estoque atualizado.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro no registro de reposição: {exc}")
            flash("Erro interno ao registrar reposição de estoque.", "danger")
        return redirect(url_for("products.stock"))

    product_rows = db.execute("SELECT * FROM products WHERE active=1 ORDER BY stock, name").fetchall()
    history = db.execute(
        """SELECT r.*, p.name product_name FROM restocks r JOIN products p ON p.id=r.product_id
        ORDER BY r.id DESC LIMIT 30"""
    ).fetchall()
    adjustments = db.execute(
        """SELECT a.*,p.name product_name,u.name user_name FROM stock_adjustments a
        JOIN products p ON p.id=a.product_id LEFT JOIN users u ON u.id=a.user_id
        ORDER BY a.id DESC LIMIT 30"""
    ).fetchall()
    return render_template("stock.html", products=product_rows, history=history, adjustments=adjustments)
