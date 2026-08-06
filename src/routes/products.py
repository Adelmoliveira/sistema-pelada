from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, send_file
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import cents
from src.services.cash_register import create_movement, get_session
from src.services.stock_report_pdf import build_stock_report_pdf, stock_report_data, build_low_stock_pdf, low_stock_report_data
from src.services.stock_alerts import notify_low_stock
from src.services.material_photos import process_material_photo
from src.utils import local_today

bp = Blueprint("products", __name__)

PRODUCT_CATEGORIES = (
    "Cerveja", "Refrigerante", "Água Mineral com gás",
    "Água Mineral sem gás", "Energético", "Suco", "Isotônico",
    "Salgadinho", "Alimentos", "Outros",
)

RESTOCK_STATUS_LABELS = {
    "PENDENTE": "Pendente",
    "VISTA": "Vista",
    "EM_PROCESSO": "Em processo de compra",
    "COMPRA_EFETUADA": "Compra efetuada",
    "ATENDIDA": "Solicitação atendida",
    "CANCELADA": "Cancelada",
}
RESTOCK_STATUS_ORDER = ("PENDENTE", "VISTA", "EM_PROCESSO", "COMPRA_EFETUADA", "ATENDIDA", "CANCELADA")


def _restock_status_options(status):
    if status in {"ATENDIDA", "CANCELADA"}:
        return []
    index = RESTOCK_STATUS_ORDER.index(status) if status in RESTOCK_STATUS_ORDER else 0
    options = []
    if index + 1 < len(RESTOCK_STATUS_ORDER):
        next_status = RESTOCK_STATUS_ORDER[index + 1]
        if next_status != "CANCELADA":
            options.append(next_status)
    options.append("CANCELADA")
    return options

@bp.route("/products", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def products():
    db = get_db()
    if request.method == "POST":
        try:
            category = request.form.get("category", "")
            if category not in PRODUCT_CATEGORIES:
                raise ValueError("Selecione uma categoria válida.")
            processed_photo = process_material_photo(request.files.get("photo"))
            photo_data, thumbnail_data = processed_photo or ("", "")
            units_per_case = int(request.form.get("units_per_case") or 0)
            loose_units = int(request.form.get("stock") or 0)
            cases = int(request.form.get("initial_cases") or 0)
            if min(units_per_case, loose_units, cases) < 0:
                raise ValueError("As quantidades não podem ser negativas.")
            if cases and not units_per_case:
                raise ValueError("Informe quantas unidades vêm em cada caixa.")
            
            initial_stock = loose_units + cases * units_per_case
            created = db.execute(
                """INSERT INTO products(name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,supplier_email,photo_data,thumbnail_data,expiry_date)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request.form["name"].strip(),
                    category,
                    request.form.get("package_type", ""),
                    units_per_case,
                    cents(request.form["price"]),
                    cents(request.form.get("cost", "0")),
                    initial_stock,
                    int(request.form.get("min_stock", 5)),
                    request.form.get("supplier_email", "").strip().lower(),
                    photo_data,
                    thumbnail_data,
                    request.form.get("expiry_date", ""),
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
    return render_template("products.html", products=items, product_categories=PRODUCT_CATEGORIES)

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
            category = request.form.get("category", "")
            if category not in PRODUCT_CATEGORIES:
                raise ValueError("Selecione uma categoria válida.")
            photo_data = product["photo_data"] or ""
            thumbnail_data = product["thumbnail_data"] or ""
            if request.form.get("remove_photo") == "1":
                photo_data, thumbnail_data = "", ""
            processed_photo = process_material_photo(request.files.get("photo"))
            if processed_photo:
                photo_data, thumbnail_data = processed_photo
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
                price_cents=?,cost_cents=?,min_stock=?,stock=?,supplier_email=?,photo_data=?,thumbnail_data=?,expiry_date=? WHERE id=?""",
                (
                    request.form["name"].strip(),
                    category,
                    request.form.get("package_type", ""),
                    units_per_case,
                    cents(request.form["price"]),
                    cents(request.form.get("cost", "0")),
                    min_stock,
                    new_stock,
                    request.form.get("supplier_email", "").strip().lower(),
                    photo_data,
                    thumbnail_data,
                    request.form.get("expiry_date", ""),
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
    return render_template("edit_product.html", product=product, product_categories=PRODUCT_CATEGORIES)

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
    alert_rows = [dict(row) for row in product_rows]
    sales = db.execute(
        """SELECT si.product_id,si.quantity,s.created_at FROM sale_items si JOIN sales s ON s.id=si.sale_id
           WHERE s.paid=1 AND s.created_at>=?""", ((local_today() - timedelta(days=35)).isoformat(),)
    ).fetchall()
    usage = {row["id"]: {"recent": 0, "prior": 0, "last": None} for row in alert_rows}
    for sale in sales:
        when = date.fromisoformat(str(sale["created_at"])[:10]); bucket = usage.get(sale["product_id"])
        if not bucket: continue
        bucket["last"] = max(bucket["last"] or when, when); age = (local_today() - when).days
        bucket["recent" if age <= 7 else "prior"] += int(sale["quantity"])
    stock_alerts = {"low": [], "dormant": [], "expiring": [], "unusual": []}
    for product in alert_rows:
        stats = usage[product["id"]]
        if int(product["stock"]) <= int(product["min_stock"]): stock_alerts["low"].append(product)
        if int(product["stock"]) > 0 and (not stats["last"] or (local_today() - stats["last"]).days >= 30): stock_alerts["dormant"].append(product)
        if product.get("expiry_date"):
            days = (date.fromisoformat(str(product["expiry_date"])[:10]) - local_today()).days
            if days <= 30: product["expiry_days"] = days; stock_alerts["expiring"].append(product)
        weekly_average = stats["prior"] / 4
        if stats["recent"] >= 5 and stats["recent"] > weekly_average * 2:
            product["recent_usage"] = stats["recent"]; stock_alerts["unusual"].append(product)
    history_total = db.execute("SELECT COUNT(*) total FROM restocks").fetchone()["total"]
    try:
        history_page = max(1, int(request.args.get("history_page", 1)))
    except (TypeError, ValueError):
        history_page = 1
    history_pages = max(1, (history_total + 5) // 6)
    history_page = min(history_page, history_pages)
    history_offset = (history_page - 1) * 6
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
        ORDER BY r.id DESC LIMIT ? OFFSET ?""",
        (6, history_offset),
    ).fetchall()
    adjustments = db.execute(
        """SELECT a.*,p.name product_name,u.name user_name FROM stock_adjustments a
        JOIN products p ON p.id=a.product_id LEFT JOIN users u ON u.id=a.user_id
        ORDER BY a.id DESC LIMIT 30"""
    ).fetchall()
    return render_template("stock.html", products=product_rows, history=history, adjustments=adjustments,
                           stock_alerts=stock_alerts,
                           history_total=history_total, history_page=history_page,
                           history_pages=history_pages, report_start=request.args.get("start", ""),
                           report_end=request.args.get("end", ""))


@bp.route("/stock/conference", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def stock_conference():
    """Register the monthly physical bar-stock count without changing stock."""
    db = get_db()
    month = (request.form.get("conference_month") if request.method == "POST" else request.args.get("month")) or local_today().strftime("%Y-%m")
    products = db.execute(
        "SELECT id,name,category,stock FROM products WHERE active=1 ORDER BY category,name"
    ).fetchall()
    if request.method == "POST":
        try:
            try:
                date.fromisoformat(f"{month}-01")
            except (TypeError, ValueError):
                raise ValueError("Informe um mês válido no formato AAAA-MM.")
            notes = request.form.get("notes", "").strip()
            counts = []
            for product in products:
                raw = (request.form.get(f"physical_{product['id']}") or "").strip()
                if raw == "":
                    raise ValueError(f"Informe a contagem física de {product['name']}.")
                physical = int(raw)
                if physical < 0:
                    raise ValueError("A contagem física não pode ser negativa.")
                expected = int(product["stock"] or 0)
                difference = physical - expected
                reason = request.form.get(f"reason_{product['id']}", "").strip()
                if difference and not reason:
                    raise ValueError(f"Informe o motivo da diferença de {product['name']}.")
                counts.append((product["id"], expected, physical, difference, reason))
            if not counts:
                raise ValueError("Não há produtos ativos para conferir.")
            with db:
                conference = db.execute(
                    "INSERT INTO stock_conferences(conference_month,notes,performed_by) VALUES(?,?,?)",
                    (month, notes, g.user["id"]),
                )
                for product_id, expected, physical, difference, reason in counts:
                    db.execute(
                        "INSERT INTO stock_conference_items(conference_id,product_id,expected_stock,physical_stock,difference,reason) VALUES(?,?,?,?,?,?)",
                        (conference.lastrowid, product_id, expected, physical, difference, reason),
                    )
                db.execute(
                    "INSERT INTO stock_conference_audit(conference_month,action,details,user_id) VALUES(?,?,?,?)",
                    (month, "REGISTRADA", f"Conferência #{conference.lastrowid} registrada.", g.user["id"]),
                )
            flash(f"Conferência de {month} registrada com sucesso.", "success")
            return redirect(url_for("products.stock_conference", month=month))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error("Erro ao registrar conferência de estoque %s: %s", month, exc)
            if "unique" in str(exc).lower():
                flash("Já existe uma conferência registrada para este mês.", "danger")
            else:
                flash("Erro interno ao registrar a conferência.", "danger")

    conferences = db.execute(
        """SELECT c.*,u.name performed_by_name,
                  (SELECT COUNT(*) FROM stock_conference_items i WHERE i.conference_id=c.id) item_count,
                  (SELECT COUNT(*) FROM stock_conference_items i WHERE i.conference_id=c.id AND i.difference<>0) difference_count,
                  (SELECT COALESCE(SUM(ABS(i.difference)),0) FROM stock_conference_items i WHERE i.conference_id=c.id) difference_units
           FROM stock_conferences c LEFT JOIN users u ON u.id=c.performed_by
           ORDER BY c.conference_month DESC,c.id DESC"""
    ).fetchall()
    selected_id = request.args.get("conference_id", type=int)
    selected = None
    selected_items = []
    if selected_id:
        selected = db.execute(
            """SELECT c.*,u.name performed_by_name,
                      EXISTS(SELECT 1 FROM stock_conference_audit a
                             WHERE a.conference_month=c.conference_month AND a.action='APLICADA') AS applied
               FROM stock_conferences c LEFT JOIN users u ON u.id=c.performed_by WHERE c.id=?""",
            (selected_id,),
        ).fetchone()
        if selected:
            selected_items = db.execute(
                """SELECT i.*,p.name product_name,p.category FROM stock_conference_items i
                   JOIN products p ON p.id=i.product_id WHERE i.conference_id=? ORDER BY p.category,p.name""",
                (selected_id,),
            ).fetchall()
    return render_template(
        "stock_conference.html", products=products, conferences=conferences,
        selected=selected, selected_items=selected_items, selected_id=selected_id,
        conference_month=month, is_manager=g.user["role"] == "manager",
    )


@bp.post("/stock/conference/<int:conference_id>/delete")
@roles_allowed("manager")
def delete_stock_conference(conference_id):
    db = get_db()
    conference = db.execute("SELECT * FROM stock_conferences WHERE id=?", (conference_id,)).fetchone()
    if not conference:
        flash("Conferência não encontrada.", "warning")
        return redirect(url_for("products.stock_conference"))
    try:
        with db:
            db.execute(
                "INSERT INTO stock_conference_audit(conference_month,action,details,user_id) VALUES(?,?,?,?)",
                (conference["conference_month"], "EXCLUIDA", f"Conferência #{conference_id} excluída.", g.user["id"]),
            )
            db.execute("DELETE FROM stock_conference_items WHERE conference_id=?", (conference_id,))
            db.execute("DELETE FROM stock_conferences WHERE id=?", (conference_id,))
        flash("Conferência excluída. A ação foi registrada na auditoria.", "success")
    except Exception as exc:
        db.rollback()
        current_app.logger.error("Erro ao excluir conferência %s: %s", conference_id, exc)
        flash("Erro interno ao excluir a conferência.", "danger")
    return redirect(url_for("products.stock_conference"))


@bp.post("/stock/conference/<int:conference_id>/apply")
@roles_allowed("manager")
def apply_stock_conference(conference_id):
    """Apply a confirmed physical count to product stock, exactly once."""
    db = get_db()
    conference = db.execute("SELECT * FROM stock_conferences WHERE id=?", (conference_id,)).fetchone()
    if not conference:
        flash("Conferência não encontrada.", "warning")
        return redirect(url_for("products.stock_conference"))
    applied = db.execute(
        "SELECT id FROM stock_conference_audit WHERE conference_month=? AND action='APLICADA' LIMIT 1",
        (conference["conference_month"],),
    ).fetchone()
    if applied:
        flash("Esta conferência já foi aplicada ao estoque.", "warning")
        return redirect(url_for("products.stock_conference", conference_id=conference_id))
    items = db.execute(
        "SELECT * FROM stock_conference_items WHERE conference_id=? ORDER BY product_id",
        (conference_id,),
    ).fetchall()
    changed_products = []
    try:
        with db:
            for item in items:
                product = db.execute("SELECT id,name,stock FROM products WHERE id=?", (item["product_id"],)).fetchone()
                if not product:
                    continue
                # Do not overwrite sales/restocks made after the count.
                if int(product["stock"] or 0) != int(item["expected_stock"] or 0):
                    raise ValueError(
                        f"O estoque de {product['name']} mudou desde a conferência. Faça uma nova conferência."
                    )
                difference = int(item["physical_stock"]) - int(product["stock"] or 0)
                if difference:
                    db.execute("UPDATE products SET stock=? WHERE id=?", (item["physical_stock"], product["id"]))
                    db.execute(
                        """INSERT INTO stock_adjustments
                        (product_id,user_id,previous_stock,new_stock,difference,reason)
                        VALUES(?,?,?,?,?,?)""",
                        (product["id"], g.user["id"], product["stock"], item["physical_stock"], difference,
                         f"Conferência mensal {conference['conference_month']}: {item['reason'] or 'ajuste de contagem física'}"),
                    )
                    changed_products.append(product["id"])
            db.execute(
                "INSERT INTO stock_conference_audit(conference_month,action,details,user_id) VALUES(?,?,?,?)",
                (conference["conference_month"], "APLICADA", f"Conferência #{conference_id} aplicada ao estoque.", g.user["id"]),
            )
        if changed_products:
            notify_low_stock(db, changed_products)
        flash("Conferência aplicada ao estoque com sucesso.", "success")
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error("Erro ao aplicar conferência %s: %s", conference_id, exc)
        flash("Erro interno ao aplicar a conferência.", "danger")
    return redirect(url_for("products.stock_conference", conference_id=conference_id))


@bp.route("/stock/restock-request", methods=["GET", "POST"])
@roles_allowed("staff", "manager")
def restock_request():
    """Formulário simples para a atendente solicitar a reposição do bar."""
    db = get_db()
    products = db.execute(
        "SELECT id,name,category,units_per_case,stock FROM products WHERE active=1 ORDER BY category,name"
    ).fetchall()
    if request.method == "POST":
        try:
            items = []
            for product in products:
                raw = (request.form.get(f"quantity_{product['id']}") or "").strip()
                if not raw:
                    continue
                quantity = int(raw)
                if quantity < 0:
                    raise ValueError("As quantidades não podem ser negativas.")
                if quantity:
                    measure = "caixas" if int(product["units_per_case"] or 0) else "unidades"
                    description = (request.form.get(f"description_{product['id']}") or "").strip()
                    items.append((product["id"], quantity, measure, description))
            cleaning = request.form.get("cleaning_materials", "").strip()
            if not items and not cleaning:
                raise ValueError("Informe ao menos um item do bar ou material de limpeza.")
            with db:
                cur = db.execute(
                    "INSERT INTO bar_restock_requests(submitted_by,cleaning_materials,workflow_status) VALUES(?,?,?)",
                    (g.user["id"], cleaning, "PENDENTE"),
                )
                db.execute("INSERT INTO bar_restock_request_history(request_id,status,notes,changed_by) VALUES(?,?,?,?)",
                           (cur.lastrowid, "PENDENTE", "Solicitação enviada.", g.user["id"]))
                for product_id, quantity, measure, description in items:
                    db.execute(
                        "INSERT INTO bar_restock_request_items(request_id,product_id,quantity,measure,description) VALUES(?,?,?,?,?)",
                        (cur.lastrowid, product_id, quantity, measure, description),
                    )
            flash("Solicitação de reposição enviada ao gerente.", "success")
            return redirect(url_for("products.restock_request"), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao solicitar reposição do bar: {exc}")
            flash("Erro interno ao enviar a solicitação.", "danger")

    own_requests = db.execute(
        """SELECT r.*,u.name submitted_by_name,ru.name reviewed_by_name
           FROM bar_restock_requests r JOIN users u ON u.id=r.submitted_by
           LEFT JOIN users ru ON ru.id=r.reviewed_by
           WHERE r.submitted_by=? ORDER BY r.id DESC LIMIT 10""",
        (g.user["id"],),
    ).fetchall()
    notifications = db.execute(
        """SELECT n.*,r.workflow_status FROM bar_restock_notifications n
           JOIN bar_restock_requests r ON r.id=n.request_id
           WHERE n.user_id=? ORDER BY n.id DESC LIMIT 20""", (g.user["id"],)
    ).fetchall()
    unread_notifications = sum(1 for notification in notifications if notification["read_at"] is None)
    if unread_notifications:
        db.execute("UPDATE bar_restock_notifications SET read_at=CURRENT_TIMESTAMP WHERE user_id=? AND read_at IS NULL", (g.user["id"],))
        db.commit()
    histories = {}
    request_ids = [row["id"] for row in own_requests]
    if request_ids:
        placeholders = ",".join("?" for _ in request_ids)
        for history in db.execute(
            f"SELECT h.*,u.name changed_by_name FROM bar_restock_request_history h JOIN users u ON u.id=h.changed_by WHERE h.request_id IN ({placeholders}) ORDER BY h.created_at DESC",
            request_ids,
        ).fetchall():
            histories.setdefault(history["request_id"], []).append(history)
    return render_template("restock_request.html", products=products, requests=own_requests,
                           notifications=notifications, unread_notifications=unread_notifications,
                           histories=histories, status_labels=RESTOCK_STATUS_LABELS)


@bp.route("/stock/restock-requests", methods=["GET", "POST"])
@roles_allowed("manager")
def restock_requests():
    """Caixa de entrada do gerente para acompanhar as reposições solicitadas."""
    db = get_db()
    if request.method == "POST":
        try:
            request_id = int(request.form["request_id"])
            status = request.form.get("status", "VISTA")
            if status not in RESTOCK_STATUS_LABELS or status == "PENDENTE":
                raise ValueError("Situação inválida.")
            current = db.execute("SELECT * FROM bar_restock_requests WHERE id=?", (request_id,)).fetchone()
            if not current:
                raise ValueError("Solicitação não encontrada.")
            current_status = current["workflow_status"] if "workflow_status" in current.keys() else current["status"]
            if status not in _restock_status_options(current_status):
                raise ValueError("Essa transição de situação não é permitida.")
            notes = request.form.get("review_notes", "").strip()
            if status == "CANCELADA" and not notes:
                raise ValueError("Informe o motivo do cancelamento.")
            legacy_status = "ATENDIDA" if status == "ATENDIDA" else ("CANCELADA" if status == "CANCELADA" else "VISTA")
            updated = db.execute(
                """UPDATE bar_restock_requests SET status=?,workflow_status=?,reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP,review_notes=?
                   WHERE id=?""",
                (legacy_status, status, g.user["id"], notes, request_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Solicitação não encontrada.")
            db.execute("INSERT INTO bar_restock_request_history(request_id,status,notes,changed_by) VALUES(?,?,?,?)",
                       (request_id, status, notes, g.user["id"]))
            db.execute("""INSERT INTO bar_restock_notifications(request_id,user_id,title,body)
                       VALUES(?,?,?,?)""", (request_id, current["submitted_by"],
                       f"Reposição #{request_id}: {RESTOCK_STATUS_LABELS[status]}",
                       notes or f"A situação da sua solicitação foi atualizada para {RESTOCK_STATUS_LABELS[status]}."))
            db.commit()
            flash("Solicitação atualizada.", "success")
        except (TypeError, ValueError) as exc:
            db.rollback()
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao atualizar solicitação de reposição: {exc}")
            flash("Erro interno ao atualizar a solicitação.", "danger")
        return redirect(url_for("products.restock_requests"), code=303)

    rows = db.execute(
        """SELECT r.*,u.name submitted_by_name,ru.name reviewed_by_name
           FROM bar_restock_requests r JOIN users u ON u.id=r.submitted_by
           LEFT JOIN users ru ON ru.id=r.reviewed_by ORDER BY r.id DESC LIMIT 50"""
    ).fetchall()
    items_by_request = {}
    for item in db.execute(
        """SELECT i.request_id,i.quantity,i.measure,i.description,p.name product_name
           FROM bar_restock_request_items i JOIN products p ON p.id=i.product_id
           WHERE i.request_id IN (SELECT id FROM bar_restock_requests ORDER BY id DESC LIMIT 50)
           ORDER BY p.name"""
    ).fetchall():
        items_by_request.setdefault(item["request_id"], []).append(item)
    histories = {}
    request_ids = [row["id"] for row in rows]
    if request_ids:
        placeholders = ",".join("?" for _ in request_ids)
        for history in db.execute(
            f"SELECT h.*,u.name changed_by_name FROM bar_restock_request_history h JOIN users u ON u.id=h.changed_by WHERE h.request_id IN ({placeholders}) ORDER BY h.created_at DESC",
            request_ids,
        ).fetchall():
            histories.setdefault(history["request_id"], []).append(history)
    return render_template("restock_requests.html", requests=rows, items_by_request=items_by_request,
                           histories=histories, status_labels=RESTOCK_STATUS_LABELS,
                           status_options={row["id"]: _restock_status_options(row["workflow_status"] if "workflow_status" in row.keys() else row["status"]) for row in rows})


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
