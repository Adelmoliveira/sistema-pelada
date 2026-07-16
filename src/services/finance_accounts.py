from src.services.cash_register import session_summary


ACCOUNT_LABELS = {"cash": "Dinheiro do Financeiro", "bank": "Banco do Financeiro"}
MOVEMENT_CATEGORY_LABELS = {
    "fundraising": "Outra arrecadação",
    "donation": "Doação",
    "expense": "Despesa administrativa",
    "adjustment": "Ajuste",
    "other": "Outro",
}
ALL_CATEGORY_LABELS = {
    "membership": "Mensalidade",
    **MOVEMENT_CATEGORY_LABELS,
    "transfer": "Transferência entre áreas",
}


def finance_account(db):
    return db.execute("SELECT * FROM finance_accounts WHERE id=1").fetchone()


def create_finance_movement(
    db, account, direction, category, amount_cents, description, created_by,
    source="manual", source_id=None, reversed_movement_id=None,
):
    if account not in ACCOUNT_LABELS:
        raise ValueError("Conta financeira inválida.")
    if direction not in {"in", "out"}:
        raise ValueError("Tipo de movimentação inválido.")
    if category not in {*MOVEMENT_CATEGORY_LABELS, "membership", "transfer"}:
        raise ValueError("Categoria financeira inválida.")
    if int(amount_cents or 0) <= 0:
        raise ValueError("O valor deve ser maior que zero.")
    if not (description or "").strip():
        raise ValueError("Informe a descrição da movimentação.")
    return db.execute(
        """INSERT INTO finance_movements
        (account,direction,category,amount_cents,description,source,source_id,
         created_by,reversed_movement_id)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            account, direction, category, int(amount_cents), description.strip(), source,
            source_id, created_by, reversed_movement_id,
        ),
    )


def finance_summary(db):
    account = finance_account(db)
    opening_cash = int(account["opening_cash_cents"] or 0) if account else 0
    opening_bank = int(account["opening_bank_cents"] or 0) if account else 0
    totals = db.execute(
        """SELECT
        COALESCE(SUM(CASE WHEN account='cash' AND direction='in' THEN amount_cents ELSE 0 END),0) cash_in,
        COALESCE(SUM(CASE WHEN account='cash' AND direction='out' THEN amount_cents ELSE 0 END),0) cash_out,
        COALESCE(SUM(CASE WHEN account='bank' AND direction='in' THEN amount_cents ELSE 0 END),0) bank_in,
        COALESCE(SUM(CASE WHEN account='bank' AND direction='out' THEN amount_cents ELSE 0 END),0) bank_out
        FROM finance_movements"""
    ).fetchone()
    cash = opening_cash + int(totals["cash_in"]) - int(totals["cash_out"])
    bank = opening_bank + int(totals["bank_in"]) - int(totals["bank_out"])
    return {
        "initialized": bool(account), "opening_cash": opening_cash, "opening_bank": opening_bank,
        "cash_in": totals["cash_in"], "cash_out": totals["cash_out"],
        "bank_in": totals["bank_in"], "bank_out": totals["bank_out"],
        "cash": cash, "bank": bank, "total": cash + bank,
    }


def latest_bar_balances(db):
    session = db.execute(
        "SELECT * FROM cash_sessions ORDER BY business_date DESC LIMIT 1"
    ).fetchone()
    if not session:
        return {"cash": 0, "bank": 0, "session": None}
    if session["status"] == "open":
        summary = session_summary(db, session)
        cash, bank = summary["expected_cash"], summary["expected_bank"]
    else:
        cash = session["counted_cash_cents"]
        bank = session["counted_bank_cents"]
        if cash is None:
            cash = session["expected_cash_cents"] or 0
        if bank is None:
            bank = session["expected_bank_cents"] or 0
    return {"cash": int(cash or 0), "bank": int(bank or 0), "session": session}


def finance_ledger_rows(db, start_date, end_date, account="", category="", query=""):
    conditions = ["date(m.created_at) BETWEEN ? AND ?"]
    params = [start_date, end_date]
    if account in ACCOUNT_LABELS:
        conditions.append("m.account=?")
        params.append(account)
    if category in ALL_CATEGORY_LABELS:
        conditions.append("m.category=?")
        params.append(category)
    if query:
        conditions.append("(LOWER(m.description) LIKE ? OR LOWER(COALESCE(u.name,'')) LIKE ?)")
        term = f"%{query.lower()}%"
        params.extend([term, term])
    movements = db.execute(
        f"""SELECT m.*,u.name user_name FROM finance_movements m
        LEFT JOIN users u ON u.id=m.created_by
        WHERE {' AND '.join(conditions)} ORDER BY m.id DESC LIMIT 1000""",
        tuple(params),
    ).fetchall()
    totals = {
        "in": sum(int(row["amount_cents"] or 0) for row in movements if row["direction"] == "in"),
        "out": sum(int(row["amount_cents"] or 0) for row in movements if row["direction"] == "out"),
    }
    totals["net"] = totals["in"] - totals["out"]
    return {"movements": movements, "totals": totals}
