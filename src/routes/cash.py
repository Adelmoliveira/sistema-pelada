from datetime import date

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.cash_register import (
    ACCOUNT_LABELS,
    CATEGORY_LABELS,
    create_movement,
    get_session,
    history_rows,
    session_summary,
)
from src.services.cash_pdf import build_cash_pdf
from src.utils import cents, local_today


bp = Blueprint("cash", __name__, url_prefix="/cash")


def _history_filters():
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
    direction = request.args.get("direction", "")
    category = request.args.get("category", "")
    query = request.args.get("q", "").strip()[:80]
    if account not in {"", *ACCOUNT_LABELS}:
        account = ""
    if direction not in {"", "in", "out"}:
        direction = ""
    if category not in {"", *CATEGORY_LABELS}:
        category = ""
    return start.isoformat(), end.isoformat(), account, direction, category, query


@bp.get("")
@roles_allowed("manager", "staff")
def dashboard():
    db = get_db()
    selected_date = request.args.get("date", local_today().isoformat())
    try:
        date.fromisoformat(selected_date)
    except ValueError:
        selected_date = local_today().isoformat()
    session = get_session(db, selected_date)
    summary = session_summary(db, session) if session else None
    history = db.execute(
        """SELECT s.*,op.name opened_by_name,cl.name closed_by_name
        FROM cash_sessions s
        LEFT JOIN users op ON op.id=s.opened_by
        LEFT JOIN users cl ON cl.id=s.closed_by
        ORDER BY s.business_date DESC LIMIT 31"""
    ).fetchall()
    previous_session = db.execute(
        """SELECT * FROM cash_sessions WHERE status='closed' AND business_date<?
        ORDER BY business_date DESC LIMIT 1""",
        (local_today().isoformat(),),
    ).fetchone()
    return render_template(
        "cash.html",
        cash_session=session,
        summary=summary,
        history=history,
        account_labels=ACCOUNT_LABELS,
        category_labels=CATEGORY_LABELS,
        today=local_today(),
        selected_date=selected_date,
        previous_session=previous_session,
    )


@bp.post("/open")
@roles_allowed("manager", "staff")
def open_register():
    db = get_db()
    try:
        if get_session(db):
            raise ValueError("O caixa de hoje já foi aberto.")
        opening_cash = cents(request.form.get("opening_cash", "0"))
        opening_bank = cents(request.form.get("opening_bank", "0"))
        if min(opening_cash, opening_bank) < 0:
            raise ValueError("Os saldos iniciais não podem ser negativos.")
        db.execute(
            """INSERT INTO cash_sessions
            (business_date,opening_cash_cents,opening_bank_cents,opened_by)
            VALUES(?,?,?,?)""",
            (local_today().isoformat(), opening_cash, opening_bank, g.user["id"]),
        )
        db.commit()
        flash("Caixa aberto com sucesso.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao abrir caixa: {exc}")
        flash("Erro interno ao abrir o caixa.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.post("/movements")
@roles_allowed("manager", "staff")
def add_movement():
    db = get_db()
    try:
        session = get_session(db)
        if not session or session["status"] != "open":
            raise ValueError("Abra o caixa de hoje antes de lançar uma movimentação.")
        if request.form.get("category") == "transfer":
            raise ValueError("Use o formulário de transferência para movimentar valores entre contas.")
        create_movement(
            db,
            session["id"],
            request.form.get("account"),
            request.form.get("direction"),
            request.form.get("category"),
            cents(request.form.get("amount", "0")),
            request.form.get("description", ""),
            g.user["id"],
        )
        db.commit()
        flash("Movimentação registrada.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao lançar movimentação de caixa: {exc}")
        flash("Erro interno ao registrar movimentação.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.post("/transfers")
@roles_allowed("manager", "staff")
def add_transfer():
    db = get_db()
    try:
        session = get_session(db)
        if not session or session["status"] != "open":
            raise ValueError("Abra o caixa de hoje antes de fazer uma transferência.")
        from_account = request.form.get("from_account")
        to_account = request.form.get("to_account")
        if from_account not in ACCOUNT_LABELS or to_account not in ACCOUNT_LABELS or from_account == to_account:
            raise ValueError("Selecione contas de origem e destino diferentes.")
        amount = cents(request.form.get("amount", "0"))
        if amount <= 0:
            raise ValueError("O valor da transferência deve ser maior que zero.")
        current = session_summary(db, session)
        available = current["expected_cash" if from_account == "cash" else "expected_bank"]
        if amount > available:
            raise ValueError(f"Saldo insuficiente em {ACCOUNT_LABELS[from_account]}.")
        description = request.form.get("description", "").strip() or "Transferência entre contas"
        with db:
            transfer = db.execute(
                """INSERT INTO cash_transfers
                (session_id,from_account,to_account,amount_cents,description,created_by)
                VALUES(?,?,?,?,?,?)""",
                (session["id"], from_account, to_account, amount, description, g.user["id"]),
            )
            create_movement(db, session["id"], from_account, "out", "transfer", amount,
                            f"Transferência para {ACCOUNT_LABELS[to_account]}: {description}", g.user["id"],
                            source="transfer_out", source_id=transfer.lastrowid)
            create_movement(db, session["id"], to_account, "in", "transfer", amount,
                            f"Transferência de {ACCOUNT_LABELS[from_account]}: {description}", g.user["id"],
                            source="transfer_in", source_id=transfer.lastrowid)
        flash("Transferência registrada nas duas contas.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao transferir valores no caixa: {exc}")
        flash("Erro interno ao registrar transferência.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.post("/close")
@roles_allowed("manager", "staff")
def close_register():
    db = get_db()
    try:
        session = get_session(db)
        if not session or session["status"] != "open":
            raise ValueError("Não há caixa aberto para fechar.")
        counted_cash = cents(request.form.get("counted_cash", "0"))
        counted_bank = cents(request.form.get("counted_bank", "0"))
        if min(counted_cash, counted_bank) < 0:
            raise ValueError("Os saldos conferidos não podem ser negativos.")
        summary = session_summary(db, session)
        db.execute(
            """UPDATE cash_sessions SET status='closed',counted_cash_cents=?,counted_bank_cents=?,
            expected_cash_cents=?,expected_bank_cents=?,cash_difference_cents=?,bank_difference_cents=?,
            closing_notes=?,closed_by=?,closed_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'""",
            (
                counted_cash,
                counted_bank,
                summary["expected_cash"],
                summary["expected_bank"],
                counted_cash - summary["expected_cash"],
                counted_bank - summary["expected_bank"],
                request.form.get("closing_notes", "").strip(),
                g.user["id"],
                session["id"],
            ),
        )
        db.commit()
        flash("Caixa fechado e conferência registrada.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao fechar caixa: {exc}")
        flash("Erro interno ao fechar o caixa.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.post("/movements/<int:movement_id>/reverse")
@roles_allowed("manager")
def reverse_movement(movement_id):
    db = get_db()
    try:
        session = get_session(db)
        if not session or session["status"] != "open":
            raise ValueError("Somente movimentações do caixa aberto podem ser estornadas.")
        movement = db.execute(
            """SELECT m.* FROM cash_movements m
            WHERE m.id=? AND m.session_id=?""",
            (movement_id, session["id"]),
        ).fetchone()
        if not movement:
            raise ValueError("Movimentação não encontrada.")
        if movement["source"] == "reversal" or movement["source"].startswith("transfer"):
            raise ValueError("Um estorno não pode ser estornado novamente.")
        already_reversed = db.execute(
            "SELECT 1 FROM cash_movements WHERE reversed_movement_id=?", (movement_id,)
        ).fetchone()
        if already_reversed:
            raise ValueError("Esta movimentação já foi estornada.")
        create_movement(
            db,
            session["id"],
            movement["account"],
            "out" if movement["direction"] == "in" else "in",
            "adjustment",
            movement["amount_cents"],
            f"Estorno: {movement['description']}",
            g.user["id"],
            source="reversal",
            source_id=movement_id,
            reversed_movement_id=movement_id,
        )
        db.commit()
        flash("Movimentação estornada; o histórico original foi preservado.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao estornar movimentação {movement_id}: {exc}")
        flash("Erro interno ao estornar movimentação.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.post("/transfers/<int:transfer_id>/reverse")
@roles_allowed("manager")
def reverse_transfer(transfer_id):
    db = get_db()
    try:
        session = get_session(db)
        transfer = db.execute(
            "SELECT * FROM cash_transfers WHERE id=?", (transfer_id,)
        ).fetchone()
        if not session or session["status"] != "open" or not transfer or transfer["session_id"] != session["id"]:
            raise ValueError("Somente transferências do caixa aberto podem ser estornadas.")
        if transfer["reversed_at"]:
            raise ValueError("Esta transferência já foi estornada.")
        description = f"Estorno da transferência: {transfer['description']}"
        with db:
            create_movement(db, session["id"], transfer["to_account"], "out", "transfer",
                            transfer["amount_cents"], description, g.user["id"],
                            source="transfer_reversal_out", source_id=transfer_id)
            create_movement(db, session["id"], transfer["from_account"], "in", "transfer",
                            transfer["amount_cents"], description, g.user["id"],
                            source="transfer_reversal_in", source_id=transfer_id)
            db.execute(
                "UPDATE cash_transfers SET reversed_at=CURRENT_TIMESTAMP,reversed_by=? WHERE id=?",
                (g.user["id"], transfer_id),
            )
        flash("Transferência estornada e saldos restaurados.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao estornar transferência {transfer_id}: {exc}")
        flash("Erro interno ao estornar transferência.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.post("/<int:session_id>/reopen")
@roles_allowed("manager")
def reopen_register(session_id):
    db = get_db()
    try:
        session = get_session(db)
        if not session or session["id"] != session_id or session["status"] != "closed":
            raise ValueError("Somente o caixa fechado de hoje pode ser reaberto.")
        db.execute(
            """UPDATE cash_sessions SET status='open',counted_cash_cents=NULL,counted_bank_cents=NULL,
            expected_cash_cents=NULL,expected_bank_cents=NULL,cash_difference_cents=NULL,
            bank_difference_cents=NULL,closing_notes='',closed_by=NULL,closed_at=NULL WHERE id=?""",
            (session_id,),
        )
        db.commit()
        flash("Caixa reaberto para correção.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao reabrir caixa {session_id}: {exc}")
        flash("Erro interno ao reabrir o caixa.", "danger")
    return redirect(url_for("cash.dashboard"), code=303)


@bp.get("/history")
@roles_allowed("manager", "staff")
def history():
    start, end, account, direction, category, query = _history_filters()
    data = history_rows(get_db(), start, end, account, direction, category, query)
    return render_template(
        "cash_history.html", data=data, start=start, end=end, account=account,
        direction=direction, category=category, query=query,
        account_labels=ACCOUNT_LABELS, category_labels=CATEGORY_LABELS,
    )


@bp.get("/history.pdf")
@roles_allowed("manager", "staff")
def history_pdf():
    start, end, account, direction, category, query = _history_filters()
    data = history_rows(get_db(), start, end, account, direction, category, query)
    filter_parts = [
        ACCOUNT_LABELS.get(account, ""),
        {"in": "Entrada", "out": "Saída"}.get(direction, ""),
        CATEGORY_LABELS.get(category, ""),
        query,
    ]
    report = build_cash_pdf(
        data, start, end, " | ".join(part for part in filter_parts if part),
        ACCOUNT_LABELS, CATEGORY_LABELS, local_today(),
    )
    return send_file(
        report, mimetype="application/pdf", as_attachment=True,
        download_name=f"caixa-{start}-a-{end}.pdf",
    )
