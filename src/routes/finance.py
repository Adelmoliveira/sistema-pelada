from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import money, brdate, month_bounds, add_months

bp = Blueprint("finance", __name__)

@bp.route("/")
@roles_allowed("manager", "staff")
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    month, start, end = month_bounds()
    metrics = db.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN date(created_at)=? AND payment_method!='Cortesia' THEN total_cents END),0) day_total,
          COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? AND payment_method!='Cortesia' THEN total_cents END),0) month_total,
          COUNT(CASE WHEN created_at>=? AND created_at<? THEN 1 END) month_sales,
          COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? AND payment_method='Débito' THEN total_cents END),0) debit_total
        FROM sales WHERE paid=1
    """, (today, start, end, start, end, start, end)).fetchone()
    
    low = db.execute("SELECT * FROM products WHERE active=1 AND stock<=min_stock ORDER BY stock, name").fetchall()
    recent = db.execute("""SELECT s.*, p.name player_name FROM sales s JOIN players p ON p.id=s.player_id
                            WHERE s.paid=1 ORDER BY s.id DESC LIMIT 8""").fetchall()
    return render_template("dashboard.html", metrics=metrics, low=low, recent=recent, month=month)

@bp.route("/reports")
@roles_allowed("manager")
def reports():
    db = get_db()
    month, start, end = month_bounds(request.args.get("month"))
    
    summary = db.execute("""SELECT COALESCE(SUM(CASE WHEN payment_method!='Cortesia' THEN total_cents END),0) revenue,
        COUNT(*) sales_count, COALESCE(SUM(CASE WHEN payment_method='Pix' THEN total_cents END),0) pix,
        COALESCE(SUM(CASE WHEN payment_method='Dinheiro' THEN total_cents END),0) cash,
        COALESCE(SUM(CASE WHEN payment_method='Débito' THEN total_cents END),0) debit,
        COALESCE(SUM(CASE WHEN payment_method='Cortesia' THEN total_cents END),0) courtesy
        FROM sales WHERE paid=1 AND created_at>=? AND created_at<?""", (start, end)).fetchone()
        
    by_product = db.execute("""SELECT p.name, SUM(i.quantity) quantity,
        SUM(i.quantity*i.unit_price_cents) total, SUM(i.quantity*(i.unit_price_cents-i.unit_cost_cents)) profit
        FROM sale_items i JOIN sales s ON s.id=i.sale_id JOIN products p ON p.id=i.product_id
        WHERE s.paid=1 AND s.created_at>=? AND s.created_at<? GROUP BY p.id, p.name ORDER BY quantity DESC""", (start, end)).fetchall()
        
    by_player = db.execute("""SELECT p.name, COUNT(s.id) purchases, SUM(s.total_cents) total
        FROM sales s JOIN players p ON p.id=s.player_id WHERE s.paid=1 AND s.created_at>=? AND s.created_at<?
        GROUP BY p.id, p.name ORDER BY total DESC""", (start, end)).fetchall()
        
    sales_rows = db.execute("""SELECT s.*, p.name player_name FROM sales s JOIN players p ON p.id=s.player_id
        WHERE s.paid=1 AND s.created_at>=? AND s.created_at<? ORDER BY s.id DESC""", (start, end)).fetchall()
        
    profit = sum(r["profit"] for r in by_product)
    report_year, due_month = int(month[:4]), int(month[5:7])
    
    contributors = db.execute("SELECT id FROM players WHERE active=1 AND membership_type='regular'").fetchall()
    
    # PERFORMANCE OPTIMIZATION: Resolving N+1 query issue for membership debts
    # Fetch all payment counts for the players in the year range in one query
    start_year_month = f"{report_year}-01"
    end_year_month = f"{report_year}-{due_month:02d}"
    paid_counts_rows = db.execute(
        "SELECT player_id, COUNT(*) FROM membership_months WHERE month>=? AND month<=? GROUP BY player_id",
        (start_year_month, end_year_month)
    ).fetchall()
    paid_counts = {row[0]: row[1] for row in paid_counts_rows}
    
    debts = []
    for player in contributors:
        paid = paid_counts.get(player["id"], 0)
        debts.append(max(0, due_month - paid))
        
    membership = {
        "up_to_date": sum(debt == 0 for debt in debts),
        "owing": sum(debt > 0 for debt in debts),
        "over_2": sum(debt > 2 for debt in debts),
        "over_4": sum(debt > 4 for debt in debts),
        "over_6": sum(debt > 6 for debt in debts),
        "active": db.execute("SELECT COUNT(*) FROM players WHERE active=1").fetchone()[0],
        "inactive": db.execute("SELECT COUNT(*) FROM players WHERE active=0").fetchone()[0],
        "exempt": db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND membership_type IN ('goalkeeper','board')").fetchone()[0],
    }
    
    return render_template("reports.html", month=month, summary=summary, by_product=by_product,
                           by_player=by_player, sales=sales_rows, profit=profit, membership=membership)

@bp.route("/finance", methods=["GET", "POST"])
@roles_allowed("manager")
def finance():
    db = get_db()
    monthly_fee = 1500
    if request.method == "POST":
        try:
            player_id = int(request.form["player_id"])
            eligible = db.execute("SELECT 1 FROM players WHERE id=? AND active=1 AND membership_type='regular'", (player_id,)).fetchone()
            if not eligible:
                raise ValueError("Este peladeiro é isento ou está inativo.")
            start_month = request.form["start_month"]
            months_count = int(request.form["months_count"])
            covered_months = add_months(start_month, months_count)
            amount = monthly_fee * months_count
            with db:
                cur = db.execute("""INSERT INTO membership_payments
                    (player_id,amount_cents,months_count,start_month,payment_method,notes)
                    VALUES(?,?,?,?,?,?)""", (player_id, amount, months_count, start_month,
                    request.form["payment_method"], request.form.get("notes", "").strip()))
                for covered_month in covered_months:
                    db.execute("INSERT INTO membership_months(payment_id,player_id,month) VALUES(?,?,?)",
                                 (cur.lastrowid, player_id, covered_month))
            flash(f"Mensalidade registrada: {months_count} mês(es), total de {money(amount)}.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao registrar mensalidade: {exc}")
            if "unique" in str(exc).lower():
                flash("Não foi possível registrar: Um ou mais meses selecionados já foram pagos por este peladeiro.", "danger")
            else:
                flash("Erro interno ao registrar mensalidade.", "danger")
        return redirect(url_for("finance.finance", year=request.args.get("year", date.today().year)))

    try:
        year = int(request.args.get("year", date.today().year))
    except ValueError:
        year = date.today().year
        
    players_rows = db.execute("SELECT * FROM players WHERE active=1 AND membership_type='regular' ORDER BY name").fetchall()
    exempt_count = db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND membership_type IN ('goalkeeper','board')").fetchone()[0]
    paid_rows = db.execute("SELECT player_id, month FROM membership_months WHERE month LIKE ?", (f"{year}-%",)).fetchall()
    
    paid_by_player = {}
    for row in paid_rows:
        paid_by_player.setdefault(row["player_id"], set()).add(int(row["month"][-2:]))
    all_status_rows = [{"player": player, "months": paid_by_player.get(player["id"], set())} for player in players_rows]
    
    try:
        members_page = max(1, int(request.args.get("members_page", 1)))
    except ValueError:
        members_page = 1
    members_per_page = 10
    members_pages = max(1, (len(all_status_rows) + members_per_page - 1) // members_per_page)
    members_page = min(members_page, members_pages)
    status_rows = all_status_rows[(members_page - 1) * members_per_page:members_page * members_per_page]
    
    try:
        history_page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        history_page = 1
    per_page = 10
    history_total = db.execute("SELECT COUNT(*) FROM membership_payments").fetchone()[0]
    history_pages = max(1, (history_total + per_page - 1) // per_page)
    history_page = min(history_page, history_pages)
    history = db.execute("""SELECT mp.*, p.name player_name FROM membership_payments mp
        JOIN players p ON p.id=mp.player_id ORDER BY mp.id DESC LIMIT ? OFFSET ?""",
        (per_page, (history_page - 1) * per_page)).fetchall()
        
    collected = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM membership_payments WHERE created_at>=? AND created_at<?",
                             (f"{year}-01-01", f"{year + 1}-01-01")).fetchone()[0]
    due_month = 12 if year < date.today().year else (date.today().month if year == date.today().year else 0)
    expected_to_date = len(players_rows) * due_month * monthly_fee
    covered_to_date = sum(sum(1 for month in row["months"] if month <= due_month) for row in all_status_rows) * monthly_fee
    
    return render_template("finance.html", players=players_rows, statuses=status_rows, history=history,
                           year=year, monthly_fee=monthly_fee, collected=collected,
                           expected=expected_to_date, outstanding=max(0, expected_to_date-covered_to_date),
                           current_month=date.today().strftime("%Y-%m"), history_page=history_page,
                           history_pages=history_pages, history_total=history_total,
                           members_page=members_page, members_pages=members_pages,
                           members_total=len(all_status_rows), exempt_count=exempt_count)

@bp.post("/finance/<int:payment_id>/delete")
@roles_allowed("manager")
def delete_membership_payment(payment_id):
    db = get_db()
    try:
        with db:
            deleted = db.execute("DELETE FROM membership_payments WHERE id=?", (payment_id,))
        flash("Recebimento apagado." if deleted.rowcount else "Recebimento não encontrado.",
              "success" if deleted.rowcount else "warning")
    except Exception as exc:
        current_app.logger.error(f"Erro ao deletar recebimento {payment_id}: {exc}")
        flash("Erro interno ao apagar o recebimento.", "danger")
    return redirect(request.referrer or url_for("finance.finance"))
