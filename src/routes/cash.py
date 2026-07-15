from datetime import date

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.cash_register import (
    ACCOUNT_LABELS,
    CATEGORY_LABELS,
    create_movement,
    get_session,
    session_summary,
)
from src.utils import cents, local_today


bp = Blueprint("cash", __name__, url_prefix="/cash")


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
        if movement["source"] == "reversal":
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
