from datetime import date, timedelta
from io import BytesIO
import base64
import re
import unicodedata

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, g, send_file, abort
from werkzeug.utils import secure_filename
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import cents
from src.services.cash_register import create_movement, get_session
from src.services.stock_report_pdf import build_stock_report_pdf, stock_report_data, build_low_stock_pdf, low_stock_report_data
from src.services.stock_alerts import notify_low_stock
from src.services.material_photos import process_material_photo
from src.utils import local_today
from src.catalog import SPORTS_MATERIAL_CATEGORY

bp = Blueprint("products", __name__)

PRODUCT_CATEGORIES = (
    "Cerveja", "Refrigerante", "Água Mineral com gás",
    "Água Mineral sem gás", "Energético", "Suco", "Isotônico",
    "Salgadinho", "Alimentos", SPORTS_MATERIAL_CATEGORY, "Outros",
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
RESTOCK_APPROVAL_EDITABLE_STATUSES = {"PENDENTE", "VISTA", "EM_PROCESSO"}

def _row_get(row, key, default=None):
    """Read a column defensively from SQLite rows and psycopg DictRows.

    Reposições criadas antes da migração podem ainda estar em uma tabela sem
    os campos de compra. Atualizações simples de status não devem depender
    desses campos opcionais.
    """
    try:
        columns = _row_columns(row)
        return row[key] if key in columns else default
    except (KeyError, IndexError, TypeError, AttributeError):
        return default


def _row_columns(row):
    """Return row column names for sqlite3.Row and PostgreSQL rows.

    Some PostgreSQL adapters expose ``keys`` as a method while others expose
    it as an attribute.  Calling ``set(row.keys)`` on the latter produces a
    set containing a method object and can make an otherwise valid update
    fail with an opaque internal-server error.
    """
    keys = getattr(row, "keys", None)
    if callable(keys):
        keys = keys()
    if keys is None:
        return set()
    return set(keys)


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


def _restock_workflow_status(row):
    return str(
        _row_get(row, "workflow_status", _row_get(row, "status", "PENDENTE"))
        or "PENDENTE"
    ).strip().upper()


def _restock_quantity(row):
    approved = _row_get(row, "approved_quantity")
    return int(approved if approved is not None else row["quantity"])


def _restock_money_label(amount_cents):
    value = f"{int(amount_cents or 0) / 100:,.2f}"
    return "R$ " + value.replace(",", "_").replace(".", ",").replace("_", ".")


def _restock_purchase_cents(value):
    """Parse the HTML number input without treating its decimal dot as thousands."""
    normalized = str(value or "0").strip()
    if "," not in normalized and "." in normalized:
        normalized = normalized.replace(".", ",")
    return cents(normalized)


def _safe_rollback(db, operation):
    """Rollback without masking the original database error.

    psycopg connections can themselves raise while recovering from a failed
    transaction (for example after a dropped serverless connection).  The
    user-facing error must remain the original operation failure, while the
    rollback problem is recorded for diagnosis.
    """
    try:
        db.rollback()
    except Exception:
        current_app.logger.exception("Falha no rollback (%s)", operation)

@bp.route("/products", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def products():
    db = get_db()
    if request.method == "POST":
        try:
            category = request.form.get("category", "")
            if category not in PRODUCT_CATEGORIES:
                raise ValueError("Selecione uma categoria válida.")
            if category == SPORTS_MATERIAL_CATEGORY:
                raise ValueError("Use o cadastro próprio de Material Esportivo.")
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

    items = db.execute(
        """SELECT id,name,category,package_type,units_per_case,price_cents,cost_cents,
                   stock,min_stock,supplier_email,thumbnail_data,expiry_date,active,created_at
            FROM products WHERE category<>?
            ORDER BY active DESC,category,name""",
        (SPORTS_MATERIAL_CATEGORY,),
    ).fetchall()
    return render_template(
        "products.html", products=items,
        product_categories=tuple(category for category in PRODUCT_CATEGORIES if category != SPORTS_MATERIAL_CATEGORY),
        catalog="bar", form_action=url_for("products.products"),
    )


COIN_MATERIAL_TYPE_CODE = "commemorative_coin"
COIN_TECHNICAL_SIZE = "Único"


def _sports_material_type(db, type_id):
    material_type = db.execute(
        "SELECT id,code FROM sports_material_types WHERE id=? AND active", (type_id,)
    ).fetchone()
    if not material_type:
        raise ValueError("Selecione um tipo de material válido.")
    return material_type


def _non_negative_stock(value, label):
    value = int(value or 0)
    if value < 0:
        raise ValueError(f"{label} não pode ser negativo.")
    return value


def _sports_variants_from_form(form, material_type_code=None):
    if material_type_code == COIN_MATERIAL_TYPE_CODE:
        return [{
            "size": COIN_TECHNICAL_SIZE,
            "stock": _non_negative_stock(form.get("coin_stock"), "O estoque"),
            "min_stock": _non_negative_stock(form.get("coin_min_stock"), "O estoque mínimo"),
            "active": True,
        }]
    sizes = form.getlist("variant_size")
    stocks = form.getlist("variant_stock")
    minimums = form.getlist("variant_min_stock")
    active_values = form.getlist("variant_active")
    variants = []
    seen = set()
    for size, stock, minimum, active in zip(sizes, stocks, minimums, active_values):
        size = " ".join(size.strip().split())
        if not size:
            continue
        key = size.casefold()
        if key in seen:
            raise ValueError(f"O tamanho {size} foi informado mais de uma vez.")
        seen.add(key)
        stock = int(stock or 0)
        minimum = int(minimum or 0)
        if stock < 0:
            raise ValueError(f"O estoque do tamanho {size} não pode ser negativo.")
        if minimum < 0:
            raise ValueError(f"O estoque mínimo do tamanho {size} não pode ser negativo.")
        variants.append({"size": size, "stock": stock, "min_stock": minimum, "active": active == "1"})
    if not variants:
        raise ValueError("Cadastre ao menos um tamanho para o material esportivo.")
    return variants


def _sports_types(db, include_inactive=False):
    where = "" if include_inactive else "WHERE active"
    return db.execute(
        f"SELECT id,code,name,active,sort_order FROM sports_material_types {where} ORDER BY sort_order,name"
    ).fetchall()


def _save_sports_config(db, product_id, type_id, variants, form):
    material_type = _sports_material_type(db, type_id)
    is_coin = material_type["code"] == COIN_MATERIAL_TYPE_CODE
    if is_coin:
        variant = variants[0]
        variants = [{"size": COIN_TECHNICAL_SIZE, "stock": variant["stock"],
                     "min_stock": variant["min_stock"], "active": True}]
    if form.get("ready_sale_enabled") != "1" and form.get("allow_backorder") != "1":
        raise ValueError("Selecione pronta entrega, encomenda ou ambas as modalidades.")
    db.execute(
        """INSERT INTO sports_product_config
           (product_id,type_id,allow_custom_name,allow_custom_number,allow_backorder,ready_sale_enabled,updated_at)
           VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)
           ON CONFLICT(product_id) DO UPDATE SET type_id=excluded.type_id,
           allow_custom_name=excluded.allow_custom_name,
           allow_custom_number=excluded.allow_custom_number,
           allow_backorder=excluded.allow_backorder,ready_sale_enabled=excluded.ready_sale_enabled,
           updated_at=CURRENT_TIMESTAMP
           RETURNING product_id""",
        (product_id, type_id, False if is_coin else form.get("allow_custom_name") == "1",
         False if is_coin else form.get("allow_custom_number") == "1",
         form.get("allow_backorder") == "1", form.get("ready_sale_enabled") == "1"),
    )
    db.execute("UPDATE sports_product_variants SET active=FALSE,updated_at=CURRENT_TIMESTAMP WHERE product_id=?", (product_id,))
    for variant in variants:
        db.execute(
            """INSERT INTO sports_product_variants(product_id,size,stock,min_stock,active)
               VALUES(?,?,?,?,?) ON CONFLICT(product_id,size) DO UPDATE SET
               stock=excluded.stock,min_stock=excluded.min_stock,active=excluded.active,
               updated_at=CURRENT_TIMESTAMP""",
            (product_id, variant["size"], variant["stock"], variant["min_stock"], variant["active"]),
        )


@bp.route("/material-esportivo", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def sports_materials():
    db = get_db()
    if request.method == "POST":
        try:
            type_id = int(request.form.get("type_id") or 0)
            material_type = _sports_material_type(db, type_id)
            variants = _sports_variants_from_form(request.form, material_type["code"])
            processed_photo = process_material_photo(request.files.get("photo"))
            photo_data, thumbnail_data = processed_photo or ("", "")
            with db:
                created = db.execute(
                    """INSERT INTO products
                       (name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,
                        supplier_email,photo_data,thumbnail_data,expiry_date,active)
                       VALUES(?,?, '',0,?,?,0,0,'',?,?,'',?)""",
                    (request.form["name"].strip(), SPORTS_MATERIAL_CATEGORY,
                     cents(request.form["price"]), cents(request.form.get("cost", "0")),
                     photo_data, thumbnail_data, int(request.form.get("active") == "1")),
                )
                _save_sports_config(db, created.lastrowid, type_id, variants, request.form)
            flash("Material esportivo cadastrado.", "success")
            return redirect(url_for("products.sports_materials"), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error("SPORTS_PRODUCT_CREATE_ERROR exception_type=%s", type(exc).__name__)
            if "unique" in str(exc).lower() and "name" in str(exc).lower():
                flash("Já existe um produto cadastrado com esse nome.", "danger")
            else:
                flash("Não foi possível cadastrar o material esportivo.", "danger")
        return redirect(url_for("products.sports_materials"), code=303)

    items = db.execute(
        """SELECT p.id,p.name,p.price_cents,p.cost_cents,p.thumbnail_data,p.active,p.created_at,
                  config.product_id configured,type.name sports_type,type.code sports_type_code,
                  config.allow_custom_name,config.allow_custom_number,config.allow_backorder,config.ready_sale_enabled,
                  COALESCE(SUM(CASE WHEN variant.active THEN variant.stock ELSE 0 END),0) variant_stock,
                  COUNT(variant.id) variant_count
           FROM products p
           LEFT JOIN sports_product_config config ON config.product_id=p.id
           LEFT JOIN sports_material_types type ON type.id=config.type_id
           LEFT JOIN sports_product_variants variant ON variant.product_id=p.id
           WHERE p.category=?
           GROUP BY p.id,p.name,p.price_cents,p.cost_cents,p.thumbnail_data,p.active,p.created_at,
                    config.product_id,type.name,type.code,config.allow_custom_name,
                    config.allow_custom_number,config.allow_backorder,config.ready_sale_enabled
           ORDER BY p.active DESC,p.name""",
        (SPORTS_MATERIAL_CATEGORY,),
    ).fetchall()
    return render_template(
        "sports_materials.html", products=items, sports_types=_sports_types(db),
        coin_type_code=COIN_MATERIAL_TYPE_CODE,
    )


@bp.route("/material-esportivo/<int:product_id>/editar", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def edit_sports_material(product_id):
    db = get_db()
    product = db.execute(
        """SELECT p.id,p.name,p.price_cents,p.cost_cents,p.photo_data,p.thumbnail_data,p.active,
                  config.type_id,config.allow_custom_name,config.allow_custom_number,config.allow_backorder,
                  config.ready_sale_enabled
           FROM products p LEFT JOIN sports_product_config config ON config.product_id=p.id
           WHERE p.id=? AND p.category=?""", (product_id, SPORTS_MATERIAL_CATEGORY),
    ).fetchone()
    if not product:
        flash("Material esportivo não encontrado.", "warning")
        return redirect(url_for("products.sports_materials"))
    if request.method == "POST":
        try:
            type_id = int(request.form.get("type_id") or 0)
            material_type = _sports_material_type(db, type_id)
            variants = _sports_variants_from_form(request.form, material_type["code"])
            photo_data, thumbnail_data = product["photo_data"] or "", product["thumbnail_data"] or ""
            if request.form.get("remove_photo") == "1":
                photo_data, thumbnail_data = "", ""
            processed_photo = process_material_photo(request.files.get("photo"))
            if processed_photo:
                photo_data, thumbnail_data = processed_photo
            with db:
                db.execute(
                    """UPDATE products SET name=?,price_cents=?,cost_cents=?,photo_data=?,thumbnail_data=?,
                       active=?,package_type='',units_per_case=0,stock=0,min_stock=0 WHERE id=?""",
                    (request.form["name"].strip(), cents(request.form["price"]),
                     cents(request.form.get("cost", "0")), photo_data, thumbnail_data,
                     int(request.form.get("active") == "1"), product_id),
                )
                _save_sports_config(db, product_id, type_id, variants, request.form)
            flash("Material esportivo atualizado.", "success")
            return redirect(url_for("products.sports_materials"), code=303)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error("SPORTS_PRODUCT_UPDATE_ERROR product_id=%s exception_type=%s", product_id, type(exc).__name__)
            flash("Não foi possível atualizar o material esportivo.", "danger")
    variants = db.execute(
        """SELECT id,size,stock,min_stock,active FROM sports_product_variants
           WHERE product_id=? ORDER BY active DESC,id""", (product_id,),
    ).fetchall()
    is_coin = any(type["id"] == product["type_id"] and type["code"] == COIN_MATERIAL_TYPE_CODE
                  for type in _sports_types(db, include_inactive=True))
    coin_variant = next((variant for variant in variants if variant["size"] == COIN_TECHNICAL_SIZE), None)
    return render_template("edit_sports_material.html", product=product, variants=variants,
                           sports_types=_sports_types(db), is_coin=is_coin,
                           coin_variant=coin_variant,
                           coin_type_code=COIN_MATERIAL_TYPE_CODE,
                           coin_technical_size=COIN_TECHNICAL_SIZE)


@bp.post("/material-esportivo/tipos")
@roles_allowed("manager")
def create_sports_material_type():
    name = " ".join(request.form.get("name", "").split())
    if len(name) < 2:
        flash("Informe o nome do novo tipo.", "danger")
        return redirect(url_for("products.sports_materials"), code=303)
    code = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    code = re.sub(r"[^a-z0-9]+", "_", code).strip("_")
    try:
        db = get_db()
        order = db.execute("SELECT COALESCE(MAX(sort_order),0)+10 next_order FROM sports_material_types").fetchone()["next_order"]
        db.execute("INSERT INTO sports_material_types(code,name,sort_order) VALUES(?,?,?)", (code, name, order))
        db.commit()
        flash("Tipo de material adicionado.", "success")
    except Exception:
        flash("Esse tipo já existe ou não pôde ser cadastrado.", "danger")
    return redirect(url_for("products.sports_materials"), code=303)

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
    return redirect(url_for("products.sports_materials") if product and product["category"] == SPORTS_MATERIAL_CATEGORY else url_for("products.products"))

@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def edit_product(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        flash("Produto não encontrado.", "warning")
        return redirect(url_for("products.products"))
    if product["category"] == SPORTS_MATERIAL_CATEGORY:
        return redirect(url_for("products.edit_sports_material", product_id=product_id), code=303)
    
    if request.method == "POST":
        try:
            original_is_sports = product["category"] == SPORTS_MATERIAL_CATEGORY
            category = SPORTS_MATERIAL_CATEGORY if original_is_sports else request.form.get("category", "")
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
            return redirect(url_for("products.sports_materials") if original_is_sports else url_for("products.products"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao editar produto {product_id}: {exc}")
            if "unique" in str(exc).lower():
                flash("Já existe outro produto com esse nome.", "danger")
            else:
                flash("Erro interno ao atualizar produto.", "danger")
        product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    categories = (SPORTS_MATERIAL_CATEGORY,) if product["category"] == SPORTS_MATERIAL_CATEGORY else tuple(category for category in PRODUCT_CATEGORIES if category != SPORTS_MATERIAL_CATEGORY)
    return render_template("edit_product.html", product=product, product_categories=categories)

@bp.route("/stock", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
def stock():
    db = get_db()
    if request.method == "POST":
        try:
            pid = int(request.form["product_id"])
            product = db.execute("SELECT * FROM products WHERE id=? AND category<>?", (pid, SPORTS_MATERIAL_CATEGORY)).fetchone()
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

    product_rows = db.execute(
        """SELECT id,name,category,package_type,units_per_case,price_cents,cost_cents,
                  stock,min_stock,supplier_email,expiry_date,active,created_at
           FROM products WHERE active=1 AND category<>? ORDER BY stock,name""",
        (SPORTS_MATERIAL_CATEGORY,),
    ).fetchall()
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
    history_total = db.execute(
        """SELECT COUNT(*) total FROM restocks r JOIN products p ON p.id=r.product_id
           WHERE p.category<>?""",
        (SPORTS_MATERIAL_CATEGORY,),
    ).fetchone()["total"]
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
        WHERE p.category<>?
        ORDER BY r.id DESC LIMIT ? OFFSET ?""",
        (SPORTS_MATERIAL_CATEGORY, 6, history_offset),
    ).fetchall()
    adjustments = db.execute(
        """SELECT a.*,p.name product_name,u.name user_name FROM stock_adjustments a
        JOIN products p ON p.id=a.product_id LEFT JOIN users u ON u.id=a.user_id
        WHERE p.category<>?
        ORDER BY a.id DESC LIMIT 30"""
        , (SPORTS_MATERIAL_CATEGORY,)
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
                request_id = cur.lastrowid
                db.execute("INSERT INTO bar_restock_request_history(request_id,status,notes,changed_by) VALUES(?,?,?,?)",
                           (request_id, "PENDENTE", "Solicitação enviada.", g.user["id"]))
                for product_id, quantity, measure, description in items:
                    db.execute(
                        "INSERT INTO bar_restock_request_items(request_id,product_id,quantity,measure,description) VALUES(?,?,?,?,?)",
                        (request_id, product_id, quantity, measure, description),
                    )
                # Notify every active manager in the same transaction as the
                # request, so the alert cannot be shown without its request.
                db.execute(
                    """INSERT INTO bar_restock_notifications(request_id,user_id,title,body)
                       SELECT ?,id,?,? FROM users WHERE role='manager' AND active=1""",
                    (request_id, f"Nova reposição solicitada #{request_id}",
                     f"{g.user['name']} enviou uma nova solicitação de reposição para análise."),
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
    items_by_request = {}
    if request_ids:
        placeholders = ",".join("?" for _ in request_ids)
        for item in db.execute(
            f"""SELECT i.*,p.name product_name FROM bar_restock_request_items i
                JOIN products p ON p.id=i.product_id
                WHERE i.request_id IN ({placeholders}) ORDER BY p.name""",
            request_ids,
        ).fetchall():
            items_by_request.setdefault(item["request_id"], []).append(item)
        for history in db.execute(
            f"SELECT h.*,u.name changed_by_name FROM bar_restock_request_history h JOIN users u ON u.id=h.changed_by WHERE h.request_id IN ({placeholders}) ORDER BY h.created_at DESC",
            request_ids,
        ).fetchall():
            histories.setdefault(history["request_id"], []).append(history)
    return render_template("restock_request.html", products=products, requests=own_requests,
                           notifications=notifications, unread_notifications=unread_notifications,
                           histories=histories, items_by_request=items_by_request,
                           status_labels=RESTOCK_STATUS_LABELS)


@bp.post("/stock/restock-requests/<int:request_id>/approval")
@roles_allowed("manager")
def update_restock_request_approval(request_id):
    """Set the amount to buy without overwriting the staff request."""
    db = get_db()
    try:
        item_id = int(request.form.get("item_id", ""))
        approved_quantity = int(request.form.get("approved_quantity", ""))
        reason = request.form.get("reason", "").strip()
        current = db.execute(
            "SELECT * FROM bar_restock_requests WHERE id=?", (request_id,)
        ).fetchone()
        item = db.execute(
            """SELECT i.*,p.name product_name FROM bar_restock_request_items i
               JOIN products p ON p.id=i.product_id
               WHERE i.id=? AND i.request_id=?""",
            (item_id, request_id),
        ).fetchone()
        if not current or not item:
            raise ValueError("Solicitação ou item não encontrado.")
        status = _restock_workflow_status(current)
        if status not in RESTOCK_APPROVAL_EDITABLE_STATUSES or _row_get(current, "purchase_recorded_at"):
            raise ValueError("A quantidade não pode ser alterada após a efetivação da compra.")
        requested_quantity = int(item["quantity"])
        if approved_quantity <= 0:
            raise ValueError("A quantidade aprovada deve ser maior que zero.")
        if approved_quantity > requested_quantity:
            raise ValueError("A quantidade aprovada não pode ser maior que a quantidade solicitada.")
        previous_approved = _row_get(item, "approved_quantity")
        if previous_approved is not None and int(previous_approved) == approved_quantity:
            raise ValueError("A quantidade aprovada não foi alterada.")
        if len(reason) < 5:
            raise ValueError("Informe uma justificativa com pelo menos 5 caracteres.")
        previous_label = (
            str(previous_approved)
            if previous_approved is not None
            else f"não definida (solicitado: {requested_quantity})"
        )
        history_notes = (
            f"Aprovação de {item['product_name']}: {previous_label} → "
            f"{approved_quantity} {item['measure']}. Justificativa: {reason}"
        )
        with db:
            updated = db.execute(
                """UPDATE bar_restock_request_items SET approved_quantity=?
                   WHERE id=? AND request_id=? AND EXISTS (
                       SELECT 1 FROM bar_restock_requests r
                       WHERE r.id=? AND r.purchase_recorded_at IS NULL
                         AND r.workflow_status IN ('PENDENTE','VISTA','EM_PROCESSO')
                   )""",
                (approved_quantity, item_id, request_id, request_id),
            )
            if updated.rowcount != 1:
                raise ValueError("A compra foi efetivada e a quantidade não pode mais ser alterada.")
            db.execute(
                """INSERT INTO bar_restock_request_history
                   (request_id,status,notes,changed_by) VALUES(?,?,?,?)""",
                (request_id, status, history_notes, g.user["id"]),
            )
        flash("Quantidade aprovada atualizada com histórico preservado.", "success")
    except (TypeError, ValueError) as exc:
        _safe_rollback(db, "ajuste da quantidade aprovada")
        flash(str(exc), "danger")
    except Exception:
        _safe_rollback(db, "ajuste da quantidade aprovada")
        current_app.logger.exception(
            "Erro ao ajustar quantidade aprovada (request_id=%s)", request_id
        )
        flash("Erro interno ao ajustar a quantidade aprovada.", "danger")
    return redirect(url_for("products.restock_requests"), code=303)


@bp.post("/stock/restock-requests/<int:request_id>/value-correction")
@roles_allowed("manager")
def correct_restock_request_value(request_id):
    """Correct the request total only when no active cash movement exists."""
    db = get_db()
    try:
        corrected_amount_cents = _restock_purchase_cents(
            request.form.get("purchase_amount", "0")
        )
        reason = request.form.get("reason", "").strip()
        current = db.execute(
            "SELECT * FROM bar_restock_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not current:
            raise ValueError("Solicitação não encontrada.")
        previous_amount_cents = int(_row_get(current, "purchase_amount_cents", 0) or 0)
        if corrected_amount_cents <= 0:
            raise ValueError("O valor corrigido deve ser maior que zero.")
        if corrected_amount_cents == previous_amount_cents:
            raise ValueError("O valor informado é igual ao valor atual da solicitação.")
        if len(reason) < 5:
            raise ValueError("Informe uma justificativa com pelo menos 5 caracteres.")
        movement = db.execute(
            """SELECT m.*,EXISTS(
                   SELECT 1 FROM cash_movements reversal
                   WHERE reversal.reversed_movement_id=m.id
               ) reversed
               FROM cash_movements m
               WHERE m.source='bar_restock_request' AND m.source_id=?
               ORDER BY m.id LIMIT 1""",
            (request_id,),
        ).fetchone()
        if movement and not movement["reversed"]:
            raise ValueError(
                "A compra possui uma movimentação financeira ativa. Corrija/estorne primeiro "
                "o lançamento do caixa e depois atualize o valor da solicitação."
            )
        status = _restock_workflow_status(current)
        history_notes = (
            f"Valor da compra corrigido: {_restock_money_label(previous_amount_cents)} → "
            f"{_restock_money_label(corrected_amount_cents)}. Justificativa: {reason}"
        )
        with db:
            updated = db.execute(
                "UPDATE bar_restock_requests SET purchase_amount_cents=? WHERE id=?",
                (corrected_amount_cents, request_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Solicitação não encontrada.")
            db.execute(
                """INSERT INTO bar_restock_request_history
                   (request_id,status,notes,changed_by) VALUES(?,?,?,?)""",
                (request_id, status, history_notes, g.user["id"]),
            )
        flash("Valor da solicitação corrigido sem alterar estoque ou caixa.", "success")
    except (TypeError, ValueError) as exc:
        _safe_rollback(db, "correção do valor da solicitação")
        flash(str(exc), "danger")
    except Exception:
        _safe_rollback(db, "correção do valor da solicitação")
        current_app.logger.exception(
            "Erro ao corrigir valor da solicitação (request_id=%s)", request_id
        )
        flash("Erro interno ao corrigir o valor da solicitação.", "danger")
    return redirect(url_for("products.restock_requests"), code=303)


@bp.route("/stock/restock-requests", methods=["GET", "POST"])
@roles_allowed("manager")
def restock_requests():
    """Caixa de entrada do gerente para acompanhar as reposições solicitadas."""
    db = get_db()
    if request.method == "POST":
        try:
            request_id = int(request.form["request_id"])
            status = str(request.form.get("status", "VISTA") or "VISTA").strip().upper()
            if status not in RESTOCK_STATUS_LABELS or status == "PENDENTE":
                raise ValueError("Situação inválida.")
            current = db.execute("SELECT * FROM bar_restock_requests WHERE id=?", (request_id,)).fetchone()
            if not current:
                raise ValueError("Solicitação não encontrada.")
            current_status = _restock_workflow_status(current)
            if status not in _restock_status_options(current_status):
                raise ValueError("Essa transição de situação não é permitida.")
            notes = request.form.get("review_notes", "").strip()
            if status == "CANCELADA" and not notes:
                raise ValueError("Informe o motivo do cancelamento.")
            purchase = status == "COMPRA_EFETUADA"
            supplier = request.form.get("supplier", "").strip()
            payment_account = request.form.get("payment_account", "bank").strip()
            purchase_amount_cents = _restock_purchase_cents(request.form.get("purchase_amount", "0")) if purchase else int(_row_get(current, "purchase_amount_cents", 0) or 0)
            if purchase:
                if not supplier:
                    raise ValueError("Informe o fornecedor da compra.")
                if purchase_amount_cents <= 0:
                    raise ValueError("Informe o valor total pago da compra.")
                if payment_account not in {"cash", "bank"}:
                    raise ValueError("Selecione uma conta de pagamento válida.")
                cash_session = get_session(db)
                if not cash_session or cash_session["status"] != "open":
                    raise ValueError("Abra o caixa de hoje antes de registrar a compra efetuada.")
                if _row_get(current, "purchase_recorded_at") or db.execute(
                    "SELECT id FROM cash_movements WHERE source=? AND source_id=? LIMIT 1",
                    ("bar_restock_request", request_id),
                ).fetchone():
                    raise ValueError("A compra desta solicitação já foi registrada.")
            receipt_data = _row_get(current, "receipt_data", "") or ""
            receipt_filename = _row_get(current, "receipt_filename", "") or ""
            receipt_mime = _row_get(current, "receipt_mime", "") or ""
            if purchase:
                upload = request.files.get("receipt")
                if upload and upload.filename:
                    raw = upload.read()
                    if len(raw) > 5 * 1024 * 1024:
                        raise ValueError("A nota/recibo deve ter no máximo 5 MB.")
                    mime = (upload.mimetype or "").lower()
                    if mime != "application/pdf" and not mime.startswith("image/"):
                        raise ValueError("Anexe uma imagem ou um arquivo PDF.")
                    receipt_data = base64.b64encode(raw).decode("ascii")
                    receipt_filename = secure_filename(upload.filename) or "recibo"
                    receipt_mime = mime
            legacy_status = "ATENDIDA" if status == "ATENDIDA" else ("CANCELADA" if status == "CANCELADA" else "VISTA")
            replenished_product_ids = []
            audit_notes = notes or (f"Compra registrada: {supplier}, {purchase_amount_cents / 100:.2f}." if purchase else "")
            submitted_by = _row_get(current, "submitted_by")
            with db:
                # Grave apenas as colunas disponíveis. Isso mantém as
                # transições VISTA/EM_PROCESSO compatíveis enquanto uma
                # instalação antiga termina a migração dos campos de compra.
                columns = _row_columns(current)
                set_parts = []
                values = []
                for column, value in (("status", legacy_status), ("workflow_status", status),
                                      ("reviewed_by", g.user["id"]), ("review_notes", notes),
                                      ("supplier", supplier), ("purchase_amount_cents", purchase_amount_cents),
                                      ("payment_account", payment_account), ("receipt_data", receipt_data),
                                      ("receipt_filename", receipt_filename), ("receipt_mime", receipt_mime)):
                    if column in columns:
                        set_parts.append(f"{column}=?")
                        values.append(value)
                for column in ("reviewed_at",):
                    if column in columns:
                        set_parts.append(f"{column}=CURRENT_TIMESTAMP")
                if purchase:
                    required = {"purchase_recorded_at", "purchase_recorded_by", "supplier", "purchase_amount_cents", "payment_account"}
                    missing = sorted(required - columns)
                    if missing:
                        raise ValueError("A estrutura do banco ainda não foi atualizada para registrar a compra: " + ", ".join(missing))
                    set_parts.extend(["purchase_recorded_at=CURRENT_TIMESTAMP", "purchase_recorded_by=?"])
                    values.append(g.user["id"])
                if not set_parts:
                    raise ValueError("A solicitação não possui campos atualizáveis no banco.")
                values.append(request_id)
                updated = db.execute(f"UPDATE bar_restock_requests SET {','.join(set_parts)} WHERE id=?", tuple(values))
                if updated.rowcount != 1:
                    raise ValueError("Solicitação não encontrada.")
                if purchase:
                    item_rows = db.execute(
                        """SELECT i.*,p.name product_name,p.units_per_case FROM bar_restock_request_items i
                           JOIN products p ON p.id=i.product_id WHERE i.request_id=? ORDER BY i.id""",
                        (request_id,),
                    ).fetchall()
                    total_units = 0
                    for item in item_rows:
                        if item["measure"] == "caixas" and not int(item["units_per_case"] or 0):
                            raise ValueError(f"O produto {item['product_name']} não possui unidades por caixa cadastradas.")
                        total_units += _restock_quantity(item) * (int(item["units_per_case"] or 0) if item["measure"] == "caixas" else 1)
                    unit_cost = purchase_amount_cents // total_units if total_units else 0
                    for item in item_rows:
                        units = _restock_quantity(item) * (int(item["units_per_case"] or 0) if item["measure"] == "caixas" else 1)
                        db.execute(
                            "INSERT INTO restocks(product_id,quantity,unit_cost_cents,notes) VALUES(?,?,?,?)",
                            (item["product_id"], units, unit_cost, f"Reposição via solicitação #{request_id} — {supplier}"),
                        )
                        db.execute("UPDATE products SET stock=stock+?,cost_cents=CASE WHEN ?>0 THEN ? ELSE cost_cents END WHERE id=?",
                                   (units, unit_cost, unit_cost, item["product_id"]))
                        replenished_product_ids.append(item["product_id"])
                    create_movement(
                        db, cash_session["id"], payment_account, "out", "purchase", purchase_amount_cents,
                        f"Compra de reposição #{request_id} — {supplier}", g.user["id"],
                        source="bar_restock_request", source_id=request_id,
                    )
            # Histórico e aviso são auxiliares. Mantê-los fora da transação
            # principal evita que uma tabela auxiliar ausente (ou uma falha
            # de FK em uma instalação parcialmente migrada) deixe a conexão
            # PostgreSQL em estado ``aborted`` e faça parecer que a alteração
            # de situação falhou. Cada gravação tem sua própria transação e
            # pode falhar sem desfazer o status/compra já confirmados.
            try:
                with db:
                    db.execute(
                        "INSERT INTO bar_restock_request_history(request_id,status,notes,changed_by) VALUES(?,?,?,?)",
                        (request_id, status, audit_notes, g.user["id"]),
                    )
            except Exception:
                current_app.logger.exception(
                    "Falha ao registrar histórico de reposição (request_id=%s, operação=INSERT history)",
                    request_id,
                )
            if submitted_by:
                try:
                    with db:
                        db.execute(
                            """INSERT INTO bar_restock_notifications(request_id,user_id,title,body)
                               VALUES(?,?,?,?)""",
                            (
                                request_id,
                                submitted_by,
                                f"Reposição #{request_id}: {RESTOCK_STATUS_LABELS[status]}",
                                audit_notes or f"A situação da sua solicitação foi atualizada para {RESTOCK_STATUS_LABELS[status]}.",
                            ),
                        )
                except Exception:
                    current_app.logger.exception(
                        "Falha ao registrar notificação de reposição (request_id=%s, operação=INSERT notification)",
                        request_id,
                    )
            # This service sends e-mail and commits its alert state; only run it
            # after the stock/cash transaction above has completed successfully.
            if replenished_product_ids:
                try:
                    notify_low_stock(db, replenished_product_ids)
                except Exception:
                    # O fluxo da reposição já foi confirmado; uma falha no
                    # alerta não deve transformar a resposta em erro interno.
                    current_app.logger.exception("Falha ao emitir alerta de estoque após reposição #%s", request_id)
            flash("Solicitação atualizada.", "success")
        except (TypeError, ValueError) as exc:
            _safe_rollback(db, "atualização de reposição")
            flash(str(exc), "danger")
        except Exception as exc:
            _safe_rollback(db, "atualização de reposição")
            current_app.logger.exception(
                "Erro ao atualizar solicitação de reposição "
                "(request_id=%s, status=%s, tipo=%s, operação=UPDATE bar_restock_requests)",
                request.form.get("request_id"), request.form.get("status"), type(exc).__name__,
            )
            flash("Erro interno ao atualizar a solicitação.", "danger")
        return redirect(url_for("products.restock_requests"), code=303)

    manager_notifications = db.execute(
        """SELECT n.*,r.workflow_status FROM bar_restock_notifications n
           JOIN bar_restock_requests r ON r.id=n.request_id
           WHERE n.user_id=? ORDER BY n.id DESC LIMIT 20""",
        (g.user["id"],),
    ).fetchall()
    unread_manager_notifications = sum(1 for notification in manager_notifications if notification["read_at"] is None)
    if unread_manager_notifications:
        db.execute(
            "UPDATE bar_restock_notifications SET read_at=CURRENT_TIMESTAMP WHERE user_id=? AND read_at IS NULL",
            (g.user["id"],),
        )
        db.commit()

    rows = db.execute(
        """SELECT r.*,u.name submitted_by_name,ru.name reviewed_by_name
           FROM bar_restock_requests r JOIN users u ON u.id=r.submitted_by
           LEFT JOIN users ru ON ru.id=r.reviewed_by ORDER BY r.id DESC LIMIT 50"""
    ).fetchall()
    items_by_request = {}
    for item in db.execute(
        """SELECT i.id,i.request_id,i.quantity,i.approved_quantity,i.measure,i.description,
                  p.name product_name
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
                           manager_notifications=manager_notifications,
                           unread_manager_notifications=unread_manager_notifications,
                           status_options={
                               row["id"]: _restock_status_options(
                                   _row_get(row, "workflow_status", _row_get(row, "status", "PENDENTE"))
                               )
                               for row in rows
                           })


@bp.get("/stock/restock-requests/<int:request_id>/receipt")
@roles_allowed("manager")
def restock_receipt(request_id):
    row = get_db().execute(
        "SELECT receipt_data,receipt_mime,receipt_filename FROM bar_restock_requests WHERE id=?",
        (request_id,),
    ).fetchone()
    if not row or not row["receipt_data"]:
        abort(404)
    try:
        payload = base64.b64decode(row["receipt_data"])
    except Exception:
        abort(404)
    return send_file(BytesIO(payload), mimetype=row["receipt_mime"] or "application/octet-stream",
                     as_attachment=False, download_name=row["receipt_filename"] or "recibo")


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
