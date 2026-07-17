import hmac
from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, send_file, g
from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.debtors_pdf import build_debtors_pdf
from src.services.email_reminders import dispatch_reminders, get_reminder_settings, outstanding_players
from src.services.cash_register import create_movement, get_session, session_summary
from src.services.finance_accounts import (
    ALL_CATEGORY_LABELS,
    ACCOUNT_LABELS as FINANCE_ACCOUNT_LABELS,
    MOVEMENT_CATEGORY_LABELS,
    create_finance_movement,
    finance_ledger_rows,
    finance_summary,
    latest_bar_balances,
)
from src.services.finance_ledger_pdf import build_finance_ledger_pdf
from src.services.monthly_sales_report import build_monthly_sales_pdf, monthly_sales_data
from src.utils import alphabetical_key, money, brdate, cents, month_bounds, add_months, local_today

bp = Blueprint("finance", __name__)


def _finance_ledger_filters():
    today = local_today()
    start_text = request.args.get("start", today.replace(day=1).isoformat())
    end_text = request.args.get("end", today.isoformat())
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        if start > end:
            raise ValueError
    except ValueError:
        start, end = today.replace(day=1), today
    account = request.args.get("account", "")
    category = request.args.get("category", "")
    query = request.args.get("q", "").strip()[:80]
    if account not in {"", *FINANCE_ACCOUNT_LABELS}:
        account = ""
    if category not in {"", *ALL_CATEGORY_LABELS}:
        category = ""
    return start.isoformat(), end.isoformat(), account, category, query

@bp.route("/")
@roles_allowed("manager", "staff")
def dashboard():
    db = get_db()
    today = local_today().isoformat()
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
        FROM sales WHERE paid=1 AND COALESCE(paid_at,created_at)>=? AND COALESCE(paid_at,created_at)<?""", (start, end)).fetchone()

    courtesy_items = db.execute(
        """SELECT COALESCE(SUM(i.quantity),0)
           FROM sale_items i JOIN sales s ON s.id=i.sale_id
           WHERE s.paid=1 AND s.payment_method='Cortesia'
             AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<?""",
        (start, end),
    ).fetchone()[0]
        
    by_product = db.execute("""SELECT p.name, SUM(i.quantity) quantity,
        SUM(CASE WHEN s.payment_method='Cortesia' THEN i.quantity ELSE 0 END) courtesy_quantity,
        SUM(CASE WHEN s.payment_method!='Cortesia' THEN i.quantity*i.unit_price_cents ELSE 0 END) total,
        SUM(CASE WHEN s.payment_method!='Cortesia' THEN i.quantity*(i.unit_price_cents-i.unit_cost_cents) ELSE 0 END) profit
        FROM sale_items i JOIN sales s ON s.id=i.sale_id JOIN products p ON p.id=i.product_id
        WHERE s.paid=1 AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<? GROUP BY p.id, p.name ORDER BY quantity DESC""", (start, end)).fetchall()
        
    by_player = db.execute("""SELECT p.name, COUNT(s.id) purchases, SUM(s.total_cents) total
        FROM sales s JOIN players p ON p.id=s.player_id
        WHERE s.paid=1 AND s.payment_method!='Cortesia' AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<?
        GROUP BY p.id, p.name ORDER BY total DESC""", (start, end)).fetchall()
        
    sales_rows = db.execute("""SELECT s.*,p.name player_name,COALESCE(s.paid_at,s.created_at) sale_date
        FROM sales s JOIN players p ON p.id=s.player_id
        WHERE s.paid=1 AND COALESCE(s.paid_at,s.created_at)>=? AND COALESCE(s.paid_at,s.created_at)<?
        ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC""", (start, end)).fetchall()
        
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
        "exempt": db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND membership_type IN ('goalkeeper','board','veteran')").fetchone()[0],
    }
    
    return render_template("reports.html", month=month, summary=summary, by_product=by_product,
                           by_player=by_player, sales=sales_rows, profit=profit,
                           courtesy_items=courtesy_items, membership=membership)


@bp.get("/reports/monthly-sales.pdf")
@roles_allowed("manager")
def monthly_sales_pdf():
    data = monthly_sales_data(get_db(), request.args.get("month"))
    report = build_monthly_sales_pdf(data, local_today())
    return send_file(
        report, mimetype="application/pdf", as_attachment=True,
        download_name=f"vendas-mensais-{data['month']}.pdf",
    )

@bp.route("/finance", methods=["GET", "POST"])
@roles_allowed("manager")
def finance():
    db = get_db()
    monthly_fee = 1500
    if request.method == "POST":
        try:
            if not db.execute("SELECT 1 FROM finance_accounts WHERE id=1").fetchone():
                raise ValueError("Cadastre os saldos iniciais no Livro-caixa antes de registrar recebimentos.")
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
                payment_method = request.form["payment_method"]
                player = db.execute("SELECT name FROM players WHERE id=?", (player_id,)).fetchone()
                create_finance_movement(
                    db, "cash" if payment_method == "Dinheiro" else "bank", "in",
                    "membership", amount, f"Mensalidade de {player['name']}", g.user["id"],
                    source="membership", source_id=cur.lastrowid,
                )
            flash(f"Mensalidade registrada: {months_count} mês(es), total de {money(amount)}.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao registrar mensalidade: {exc}")
            if "unique" in str(exc).lower():
                flash("Não foi possível registrar: Um ou mais meses selecionados já foram pagos por este peladeiro.", "danger")
            else:
                flash("Erro interno ao registrar mensalidade.", "danger")
        return redirect(url_for("finance.finance", year=request.args.get("year", local_today().year)))

    try:
        year = int(request.args.get("year", local_today().year))
    except ValueError:
        year = local_today().year
        
    players_rows = db.execute("SELECT * FROM players WHERE active=1 AND membership_type='regular'").fetchall()
    players_rows = sorted(players_rows, key=lambda player: alphabetical_key(player["name"]))
    exempt_count = db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND membership_type IN ('goalkeeper','board','veteran')").fetchone()[0]
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
    today = local_today()
    due_month = 12 if year < today.year else (today.month if year == today.year else 0)
    expected_to_date = len(players_rows) * due_month * monthly_fee
    covered_to_date = sum(sum(1 for month in row["months"] if month <= due_month) for row in all_status_rows) * monthly_fee
    
    return render_template("finance.html", players=players_rows, statuses=status_rows, history=history,
                           year=year, monthly_fee=monthly_fee, collected=collected,
                           expected=expected_to_date, outstanding=max(0, expected_to_date-covered_to_date),
                           current_month=local_today().strftime("%Y-%m"), history_page=history_page,
                           history_pages=history_pages, history_total=history_total,
                           members_page=members_page, members_pages=members_pages,
                           members_total=len(all_status_rows), exempt_count=exempt_count,
                           finance_account_initialized=bool(db.execute("SELECT 1 FROM finance_accounts WHERE id=1").fetchone()))

@bp.post("/finance/<int:payment_id>/delete")
@roles_allowed("manager")
def delete_membership_payment(payment_id):
    db = get_db()
    try:
        payment = db.execute(
            "SELECT mp.*,p.name player_name FROM membership_payments mp JOIN players p ON p.id=mp.player_id WHERE mp.id=?",
            (payment_id,),
        ).fetchone()
        if not payment:
            flash("Recebimento não encontrado.", "warning")
            return redirect(request.referrer or url_for("finance.finance"))
        with db:
            original = db.execute(
                "SELECT * FROM finance_movements WHERE source='membership' AND source_id=?",
                (payment_id,),
            ).fetchone()
            if original:
                create_finance_movement(
                    db, original["account"], "out", "adjustment", original["amount_cents"],
                    f"Estorno de mensalidade apagada: {payment['player_name']}", g.user["id"],
                    source="membership_reversal", source_id=payment_id,
                    reversed_movement_id=original["id"],
                )
            db.execute("DELETE FROM membership_payments WHERE id=?", (payment_id,))
        flash("Recebimento apagado e saldo financeiro estornado.", "success")
    except Exception as exc:
        current_app.logger.error(f"Erro ao deletar recebimento {payment_id}: {exc}")
        flash("Erro interno ao apagar o recebimento.", "danger")
    return redirect(request.referrer or url_for("finance.finance"))


@bp.get("/finance/accounts")
@roles_allowed("manager")
def legacy_finance_accounts():
    return redirect(url_for("finance.finance_ledger"), code=302)


@bp.get("/finance/ledger")
@roles_allowed("manager")
def finance_ledger():
    db = get_db()
    start, end, account, category, query = _finance_ledger_filters()
    summary = finance_summary(db)
    bar = latest_bar_balances(db)
    ledger = finance_ledger_rows(db, start, end, account, category, query)
    transfers = db.execute(
        """SELECT t.*,u.name user_name FROM interaccount_transfers t
        LEFT JOIN users u ON u.id=t.created_by ORDER BY t.id DESC LIMIT 50"""
    ).fetchall()
    return render_template(
        "finance_accounts.html", summary=summary, bar=bar, ledger=ledger,
        transfers=transfers, finance_account_labels=FINANCE_ACCOUNT_LABELS,
        movement_category_labels=MOVEMENT_CATEGORY_LABELS, all_category_labels=ALL_CATEGORY_LABELS,
        consolidated_bank=summary["bank"] + bar["bank"],
        consolidated_cash=summary["cash"] + bar["cash"],
        start=start, end=end, account=account, category=category, query=query,
    )


@bp.post("/finance/ledger/initialize")
@roles_allowed("manager")
def initialize_finance_accounts():
    db = get_db()
    try:
        if db.execute("SELECT 1 FROM finance_accounts WHERE id=1").fetchone():
            raise ValueError("Os saldos iniciais do Financeiro já foram cadastrados.")
        opening_cash = cents(request.form.get("opening_cash", "0"))
        opening_bank = cents(request.form.get("opening_bank", "0"))
        if min(opening_cash, opening_bank) < 0:
            raise ValueError("Os saldos iniciais não podem ser negativos.")
        db.execute(
            """INSERT INTO finance_accounts
            (id,opening_cash_cents,opening_bank_cents,created_by) VALUES(1,?,?,?)""",
            (opening_cash, opening_bank, g.user["id"]),
        )
        db.commit()
        flash("Saldos iniciais do Financeiro cadastrados.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao iniciar contas financeiras: {exc}")
        flash("Erro interno ao cadastrar os saldos iniciais.", "danger")
    return redirect(url_for("finance.finance_ledger"), code=303)


@bp.post("/finance/ledger/movements")
@roles_allowed("manager")
def add_finance_movement():
    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM finance_accounts WHERE id=1").fetchone():
            raise ValueError("Cadastre os saldos iniciais do Financeiro antes de fazer lançamentos.")
        direction = request.form.get("direction")
        account = request.form.get("account")
        amount = cents(request.form.get("amount", "0"))
        if direction == "out":
            available = finance_summary(db)["cash" if account == "cash" else "bank"]
            if amount > available:
                raise ValueError(f"Saldo insuficiente em {FINANCE_ACCOUNT_LABELS.get(account, 'conta selecionada')}.")
        create_finance_movement(
            db, account, direction, request.form.get("category"), amount,
            request.form.get("description", ""), g.user["id"],
        )
        db.commit()
        flash("Movimentação financeira registrada.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao lançar movimentação financeira: {exc}")
        flash("Erro interno ao registrar a movimentação.", "danger")
    return redirect(url_for("finance.finance_ledger"), code=303)


@bp.post("/finance/ledger/transfers")
@roles_allowed("manager")
def transfer_finance_bar():
    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM finance_accounts WHERE id=1").fetchone():
            raise ValueError("Cadastre os saldos iniciais do Financeiro antes de transferir.")
        cash_session = get_session(db)
        if not cash_session or cash_session["status"] != "open":
            raise ValueError("Abra o caixa do Bar antes de transferir valores entre as áreas.")
        direction = request.form.get("direction")
        if direction not in {"finance_to_bar", "bar_to_finance"}:
            raise ValueError("Sentido da transferência inválido.")
        amount = cents(request.form.get("amount", "0"))
        if amount <= 0:
            raise ValueError("O valor deve ser maior que zero.")
        available = finance_summary(db)["bank"] if direction == "finance_to_bar" else session_summary(db, cash_session)["expected_bank"]
        if amount > available:
            origin = "Banco do Financeiro" if direction == "finance_to_bar" else "Banco do Bar"
            raise ValueError(f"Saldo insuficiente no {origin}.")
        description = request.form.get("description", "").strip() or "Transferência entre Financeiro e Bar"
        with db:
            transfer = db.execute(
                """INSERT INTO interaccount_transfers
                (cash_session_id,direction,amount_cents,description,created_by)
                VALUES(?,?,?,?,?)""",
                (cash_session["id"], direction, amount, description, g.user["id"]),
            )
            finance_direction = "out" if direction == "finance_to_bar" else "in"
            bar_direction = "in" if direction == "finance_to_bar" else "out"
            create_finance_movement(
                db, "bank", finance_direction, "transfer", amount, description, g.user["id"],
                source="interaccount_transfer", source_id=transfer.lastrowid,
            )
            create_movement(
                db, cash_session["id"], "bank", bar_direction, "transfer", amount,
                description, g.user["id"], source="finance_transfer", source_id=transfer.lastrowid,
            )
        flash("Transferência registrada nas contas do Financeiro e do Bar.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao transferir entre Financeiro e Bar: {exc}")
        flash("Erro interno ao registrar a transferência.", "danger")
    return redirect(url_for("finance.finance_ledger"), code=303)


@bp.post("/finance/ledger/movements/<int:movement_id>/reverse")
@roles_allowed("manager")
def reverse_finance_movement(movement_id):
    db = get_db()
    try:
        reason = request.form.get("reason", "").strip()
        if len(reason) < 5:
            raise ValueError("Informe o motivo do estorno (mínimo de 5 caracteres).")
        movement = db.execute(
            "SELECT * FROM finance_movements WHERE id=?", (movement_id,)
        ).fetchone()
        if not movement:
            raise ValueError("Movimentação financeira não encontrada.")
        if movement["source"] == "membership":
            raise ValueError("Mensalidades devem ser estornadas pela tela de Visão financeira.")
        if movement["source"] == "interaccount_transfer":
            raise ValueError("Use a ação de estorno da transferência entre áreas.")
        already_reversed = db.execute(
            "SELECT 1 FROM finance_movements WHERE reversed_movement_id=?", (movement_id,)
        ).fetchone()
        if already_reversed:
            raise ValueError("Esta movimentação já foi estornada.")
        description = f"Estorno: {movement['description']} | Motivo: {reason}"
        with db:
            create_finance_movement(
                db, movement["account"], "out" if movement["direction"] == "in" else "in",
                "adjustment", movement["amount_cents"], description, g.user["id"],
                source="finance_reversal", source_id=movement_id,
                reversed_movement_id=movement_id,
            )
        flash("Movimentação estornada com auditoria; o lançamento original foi preservado.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao estornar movimentação financeira {movement_id}: {exc}")
        flash("Erro interno ao estornar movimentação financeira.", "danger")
    return redirect(url_for("finance.finance_ledger"), code=303)


@bp.post("/finance/ledger/transfers/<int:transfer_id>/reverse")
@roles_allowed("manager")
def reverse_finance_bar_transfer(transfer_id):
    db = get_db()
    try:
        reason = request.form.get("reason", "").strip()
        if len(reason) < 5:
            raise ValueError("Informe o motivo do estorno (mínimo de 5 caracteres).")
        transfer = db.execute(
            "SELECT * FROM interaccount_transfers WHERE id=?", (transfer_id,)
        ).fetchone()
        if not transfer:
            raise ValueError("Transferência entre áreas não encontrada.")
        if transfer["reversed_at"]:
            raise ValueError("Esta transferência já foi estornada.")
        cash_session = db.execute(
            "SELECT * FROM cash_sessions WHERE id=?", (transfer["cash_session_id"],)
        ).fetchone()
        if not cash_session or cash_session["status"] != "open":
            raise ValueError("Abra o caixa do Bar para estornar esta transferência.")
        finance_movement = db.execute(
            """SELECT * FROM finance_movements
               WHERE source='interaccount_transfer' AND source_id=?""",
            (transfer_id,),
        ).fetchone()
        cash_movement = db.execute(
            """SELECT * FROM cash_movements
               WHERE source='finance_transfer' AND source_id=?""",
            (transfer_id,),
        ).fetchone()
        if not finance_movement or not cash_movement:
            raise ValueError("A transferência não possui os lançamentos correspondentes para estorno.")
        description = f"Estorno da transferência: {transfer['description']} | Motivo: {reason}"
        with db:
            create_finance_movement(
                db, finance_movement["account"],
                "out" if finance_movement["direction"] == "in" else "in",
                "transfer", finance_movement["amount_cents"], description, g.user["id"],
                source="interaccount_transfer_reversal", source_id=transfer_id,
                reversed_movement_id=finance_movement["id"],
            )
            create_movement(
                db, cash_session["id"], cash_movement["account"],
                "out" if cash_movement["direction"] == "in" else "in",
                "transfer", cash_movement["amount_cents"], description, g.user["id"],
                source="finance_transfer_reversal", source_id=transfer_id,
                reversed_movement_id=cash_movement["id"],
            )
            db.execute(
                "UPDATE interaccount_transfers SET reversed_at=CURRENT_TIMESTAMP,reversed_by=? WHERE id=?",
                (g.user["id"], transfer_id),
            )
        flash("Transferência estornada nas duas áreas com auditoria.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao estornar transferência financeira {transfer_id}: {exc}")
        flash("Erro interno ao estornar transferência.", "danger")
    return redirect(url_for("finance.finance_ledger"), code=303)


@bp.get("/finance/ledger.pdf")
@roles_allowed("manager")
def finance_ledger_pdf():
    db = get_db()
    start, end, account, category, query = _finance_ledger_filters()
    summary = finance_summary(db)
    bar = latest_bar_balances(db)
    ledger = finance_ledger_rows(db, start, end, account, category, query)
    filters = [FINANCE_ACCOUNT_LABELS.get(account, ""), ALL_CATEGORY_LABELS.get(category, ""), query]
    report = build_finance_ledger_pdf(
        ledger, summary, bar, start, end, " | ".join(value for value in filters if value),
        FINANCE_ACCOUNT_LABELS, ALL_CATEGORY_LABELS, local_today(),
    )
    return send_file(
        report, mimetype="application/pdf", as_attachment=True,
        download_name=f"livro-caixa-financeiro-{start}-a-{end}.pdf",
    )


def smtp_configuration():
    return (
        current_app.config.get("GMAIL_SMTP_USER"),
        current_app.config.get("GMAIL_APP_PASSWORD"),
    )


@bp.get("/finance/reminders")
@roles_allowed("manager")
def reminders():
    db = get_db()
    settings = get_reminder_settings(db)
    today = local_today()
    debtors = outstanding_players(db, today)
    history = db.execute(
        """SELECT rd.*,p.name player_name FROM reminder_dispatches rd
           JOIN players p ON p.id=rd.player_id ORDER BY rd.id DESC LIMIT 50"""
    ).fetchall()
    sender, password = smtp_configuration()
    return render_template(
        "reminders.html", settings=settings, debtors=debtors, history=history,
        smtp_ready=bool(sender and password), sender=sender or "diretoriagpcta@gmail.com",
        today=today,
    )


@bp.get("/finance/reminders/debtors.pdf")
@roles_allowed("manager")
def debtors_pdf():
    today = local_today()
    report = build_debtors_pdf(outstanding_players(get_db(), today), today)
    return send_file(
        report,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"devedores-{today.isoformat()}.pdf",
    )


@bp.post("/finance/reminders/settings")
@roles_allowed("manager")
def save_reminder_settings():
    db = get_db()
    settings = get_reminder_settings(db)
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    try:
        day = int(request.form.get("schedule_day", "5"))
        if not 1 <= day <= 28:
            raise ValueError
    except ValueError:
        flash("O dia do envio deve estar entre 1 e 28.", "danger")
        return redirect(url_for("finance.reminders"))
    if not subject or not body:
        flash("Assunto e mensagem são obrigatórios.", "danger")
        return redirect(url_for("finance.reminders"))
    db.execute(
        """UPDATE reminder_settings SET enabled=?,schedule_day=?,subject=?,body=?,
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (1 if request.form.get("enabled") == "1" else 0, day, subject, body, settings["id"]),
    )
    db.commit()
    flash("Configuração dos lembretes salva.", "success")
    return redirect(url_for("finance.reminders"))


@bp.post("/finance/reminders/send-now")
@roles_allowed("manager")
def send_reminders_now():
    sender, password = smtp_configuration()
    if not sender or not password:
        flash("Configure GMAIL_SMTP_USER e GMAIL_APP_PASSWORD na Vercel antes de enviar.", "danger")
        return redirect(url_for("finance.reminders"))
    result = dispatch_reminders(get_db(), get_reminder_settings(get_db()), sender, password, local_today())
    category = "warning" if result["failed"] else "success"
    flash(
        f"Envio concluído: {result['sent']} enviado(s), {result['skipped']} já enviado(s), "
        f"{result['without_email']} sem e-mail e {result['failed']} falha(s).",
        category,
    )
    return redirect(url_for("finance.reminders"))


@bp.get("/cron/payment-reminders")
def payment_reminders_cron():
    secret = current_app.config.get("CRON_SECRET") or ""
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if not secret or not hmac.compare_digest(authorization, expected):
        return jsonify(error="Não autorizado."), 401

    db = get_db()
    settings = get_reminder_settings(db)
    today = local_today()
    if not settings["enabled"]:
        return jsonify(ok=True, sent=0, reason="Lembretes desativados.")
    if today.day != settings["schedule_day"]:
        return jsonify(ok=True, sent=0, reason="Fora do dia programado.")
    sender, password = smtp_configuration()
    if not sender or not password:
        current_app.logger.error("Lembretes habilitados sem configuração SMTP completa.")
        return jsonify(error="Configuração de e-mail incompleta."), 503
    result = dispatch_reminders(db, settings, sender, password, today)
    return jsonify(ok=result["failed"] == 0, **result), 200 if result["failed"] == 0 else 502
