from datetime import date

from flask import Blueprint, render_template, request, send_file

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.finance_accounts import finance_summary, latest_bar_balances
from src.services.reports_pdf import build_membership_report_pdf, build_sale_detail_pdf
from src.services.stock_report_pdf import stock_report_data
from src.routes.maintenance import CATEGORIES, MAINTENANCE_AREAS, PRIORITIES, STATUSES
from src.utils import local_today

bp = Blueprint("reports", __name__, url_prefix="/relatorios")


def _period():
    today = local_today()
    try:
        start = date.fromisoformat(request.args.get("start", today.replace(day=1).isoformat()))
        end = date.fromisoformat(request.args.get("end", today.isoformat()))
        if start > end:
            raise ValueError
    except ValueError:
        start, end = today.replace(day=1), today
    return start.isoformat(), end.isoformat()


@bp.get("")
@roles_allowed("manager")
def index():
    return render_template("reports_hub.html")


def _membership_data(db, start, end, player_id="", payment_method=""):
    conditions, params = ["date(mp.created_at) BETWEEN ? AND ?"], [start, end]
    if player_id:
        conditions.append("mp.player_id=?")
        params.append(int(player_id))
    if payment_method:
        conditions.append("mp.payment_method=?")
        params.append(payment_method)
    rows = db.execute(
        f"""SELECT mp.*,p.name player_name,p.war_name FROM membership_payments mp
            JOIN players p ON p.id=mp.player_id WHERE {' AND '.join(conditions)}
            ORDER BY mp.created_at DESC,mp.id DESC""", tuple(params)
    ).fetchall()
    paid_players = {row["player_id"] for row in db.execute("SELECT DISTINCT player_id FROM membership_payments WHERE date(created_at) BETWEEN ? AND ?", (start, end)).fetchall()}
    pending = db.execute("SELECT id,name,war_name FROM players WHERE active=1 AND membership_type='regular' ORDER BY LOWER(name)").fetchall()
    pending = [row for row in pending if row["id"] not in paid_players]
    exempt = db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND membership_type IN ('goalkeeper','board','veteran','collaborator')").fetchone()[0]
    return rows, {"count": len(rows), "received": sum(int(row["amount_cents"] or 0) for row in rows), "exempt": int(exempt), "pending": pending}


@bp.get("/mensalidades")
@roles_allowed("manager")
def memberships():
    db = get_db()
    start, end = _period()
    player_id, payment_method = request.args.get("player_id", ""), request.args.get("payment_method", "")
    rows, summary = _membership_data(db, start, end, player_id, payment_method)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 5
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    page_rows = rows[(page - 1) * per_page:page * per_page]
    players = db.execute("SELECT id,name,war_name FROM players WHERE active=1 ORDER BY LOWER(name)").fetchall()
    return render_template("report_memberships.html", rows=page_rows, summary=summary, players=players, start=start, end=end, player_id=player_id, payment_method=payment_method, page=page, pages=pages)


@bp.get("/mensalidades.pdf")
@roles_allowed("manager")
def memberships_pdf():
    db = get_db()
    start, end = _period()
    rows, summary = _membership_data(db, start, end, request.args.get("player_id", ""), request.args.get("payment_method", ""))
    return send_file(build_membership_report_pdf(rows, summary, f"{start} a {end}"), mimetype="application/pdf", as_attachment=False, download_name=f"mensalidades-{start}-{end}.pdf")


def _sales_data(db, start, end, player_id="", payment_method=""):
    conditions, params = ["s.paid=1", "date(COALESCE(s.paid_at,s.created_at)) BETWEEN ? AND ?"], [start, end]
    if player_id:
        conditions.append("s.player_id=?")
        params.append(int(player_id))
    if payment_method:
        conditions.append("s.payment_method=?")
        params.append(payment_method)
    clause = " AND ".join(conditions)
    rows = db.execute(f"SELECT s.*,p.name player_name,p.war_name,COALESCE(s.paid_at,s.created_at) sale_date FROM sales s JOIN players p ON p.id=s.player_id WHERE {clause} ORDER BY sale_date DESC,s.id DESC", tuple(params)).fetchall()
    by_payment = db.execute(f"SELECT s.payment_method,COUNT(*) count,COALESCE(SUM(s.total_cents),0) total FROM sales s WHERE {clause} GROUP BY s.payment_method ORDER BY total DESC", tuple(params)).fetchall()
    by_product = db.execute(f"SELECT p.name,SUM(i.quantity) quantity,COALESCE(SUM(i.quantity*i.unit_price_cents),0) total FROM sale_items i JOIN sales s ON s.id=i.sale_id JOIN products p ON p.id=i.product_id WHERE {clause} GROUP BY p.id,p.name ORDER BY quantity DESC,total DESC", tuple(params)).fetchall()
    by_player = db.execute(f"SELECT p.name,COUNT(s.id) purchases,COALESCE(SUM(s.total_cents),0) total FROM sales s JOIN players p ON p.id=s.player_id WHERE {clause} GROUP BY p.id,p.name ORDER BY total DESC", tuple(params)).fetchall()
    return rows, by_payment, by_product, by_player


@bp.get("/vendas")
@roles_allowed("manager")
def sales_report():
    db = get_db()
    start, end = _period()
    player_id, payment_method = request.args.get("player_id", ""), request.args.get("payment_method", "")
    rows, by_payment, by_product, by_player = _sales_data(db, start, end, player_id, payment_method)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 5
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    page_rows = rows[(page - 1) * per_page:page * per_page]
    players = db.execute("SELECT id,name,war_name FROM players WHERE active=1 ORDER BY LOWER(name)").fetchall()
    return render_template("report_sales.html", rows=page_rows, total_rows=len(rows), by_payment=by_payment, by_product=by_product, by_player=by_player, players=players, start=start, end=end, player_id=player_id, payment_method=payment_method, page=page, pages=pages)


@bp.get("/vendas/<int:sale_id>")
@roles_allowed("manager")
def sale_detail(sale_id):
    db = get_db()
    sale = db.execute("SELECT s.*,p.name player_name,p.cpf,COALESCE(s.paid_at,s.created_at) sale_date FROM sales s JOIN players p ON p.id=s.player_id WHERE s.id=?", (sale_id,)).fetchone()
    if not sale:
        return "Venda não encontrada", 404
    items = db.execute("SELECT i.*,p.name product_name FROM sale_items i JOIN products p ON p.id=i.product_id WHERE i.sale_id=? ORDER BY i.id", (sale_id,)).fetchall()
    return render_template("report_sale_detail.html", sale=sale, items=items)


@bp.get("/vendas/<int:sale_id>.pdf")
@roles_allowed("manager")
def sale_detail_pdf(sale_id):
    db = get_db()
    sale = db.execute("SELECT s.*,p.name player_name,COALESCE(s.paid_at,s.created_at) sale_date FROM sales s JOIN players p ON p.id=s.player_id WHERE s.id=?", (sale_id,)).fetchone()
    if not sale:
        return "Venda não encontrada", 404
    items = db.execute("SELECT i.*,p.name product_name FROM sale_items i JOIN products p ON p.id=i.product_id WHERE i.sale_id=? ORDER BY i.id", (sale_id,)).fetchall()
    return send_file(build_sale_detail_pdf(sale, items), mimetype="application/pdf", as_attachment=False, download_name=f"venda-{sale_id}.pdf")


@bp.get("/consolidado")
@roles_allowed("manager")
def consolidated():
    db = get_db()
    start, end = _period()
    sales = db.execute("SELECT COALESCE(SUM(total_cents),0) total,COUNT(*) count FROM sales WHERE paid=1 AND date(COALESCE(paid_at,created_at)) BETWEEN ? AND ?", (start, end)).fetchone()
    memberships = db.execute("SELECT COALESCE(SUM(amount_cents),0) total,COUNT(*) count FROM membership_payments WHERE date(created_at) BETWEEN ? AND ?", (start, end)).fetchone()
    finance = db.execute("SELECT COALESCE(SUM(CASE WHEN direction='in' THEN amount_cents ELSE 0 END),0) incoming,COALESCE(SUM(CASE WHEN direction='out' THEN amount_cents ELSE 0 END),0) outgoing FROM finance_movements WHERE date(created_at) BETWEEN ? AND ?", (start, end)).fetchone()
    return render_template("report_consolidated.html", start=start, end=end, sales=sales, memberships=memberships, finance=finance, balance=finance_summary(db), bar=latest_bar_balances(db))


@bp.get("/estoque")
@roles_allowed("manager")
def stock_report():
    db = get_db()
    start, end = _period()
    rows = stock_report_data(db, start, end)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 5
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    page_rows = rows[(page - 1) * per_page:page * per_page]
    low_stock = db.execute("SELECT name,stock,min_stock FROM products WHERE active=1 AND stock<=min_stock ORDER BY stock,LOWER(name)").fetchall()
    return render_template("report_stock.html", rows=page_rows, total_rows=len(rows), low_stock=low_stock, start=start, end=end, page=page, pages=pages)


@bp.get("/manutencao")
@roles_allowed("manager")
def maintenance_report():
    db = get_db()
    start, end = _period()
    area, category, status = request.args.get("area", ""), request.args.get("category", ""), request.args.get("status", "")
    conditions, params = ["date(mr.created_at) BETWEEN ? AND ?"], [start, end]
    for field, value, options in (("area_code", area, MAINTENANCE_AREAS), ("category", category, CATEGORIES), ("status", status, STATUSES)):
        if value in options:
            conditions.append(f"mr.{field}=?")
            params.append(value)
    rows = db.execute(f"""SELECT mr.*,COALESCE(u.name,'') requester_name
        FROM maintenance_requests mr LEFT JOIN users u ON u.id=mr.created_by
        WHERE {' AND '.join(conditions)} ORDER BY mr.id DESC""", tuple(params)).fetchall()
    summary = {"total": len(rows), "open": sum(row["status"] != "completed" for row in rows), "completed": sum(row["status"] == "completed" for row in rows), "urgent": sum(row["priority"] == "urgent" for row in rows)}
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 5
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    page_rows = rows[(page - 1) * per_page:page * per_page]
    return render_template("report_maintenance.html", rows=page_rows, total_rows=len(rows), summary=summary, areas=MAINTENANCE_AREAS, categories=CATEGORIES, priorities=PRIORITIES, statuses=STATUSES, start=start, end=end, area=area, category=category, status=status, page=page, pages=pages)
