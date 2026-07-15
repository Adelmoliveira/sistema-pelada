from src.utils import local_today


ACCOUNT_LABELS = {"cash": "Dinheiro físico", "bank": "Conta / Pix"}
CATEGORY_LABELS = {
    "adjustment": "Ajuste",
    "deposit": "Depósito / aporte",
    "expense": "Despesa",
    "purchase": "Compra de estoque",
    "transfer": "Transferência",
    "withdrawal": "Retirada",
    "other": "Outro",
}


def get_session(db, business_date=None):
    business_date = business_date or local_today().isoformat()
    return db.execute(
        """SELECT s.*,op.name opened_by_name,cl.name closed_by_name
        FROM cash_sessions s
        LEFT JOIN users op ON op.id=s.opened_by
        LEFT JOIN users cl ON cl.id=s.closed_by
        WHERE s.business_date=?""",
        (business_date,),
    ).fetchone()


def create_movement(
    db,
    session_id,
    account,
    direction,
    category,
    amount_cents,
    description,
    created_by,
    source="manual",
    source_id=None,
    reversed_movement_id=None,
):
    if account not in ACCOUNT_LABELS:
        raise ValueError("Conta do caixa inválida.")
    if direction not in {"in", "out"}:
        raise ValueError("Tipo de movimentação inválido.")
    if category not in CATEGORY_LABELS:
        raise ValueError("Categoria inválida.")
    if int(amount_cents or 0) <= 0:
        raise ValueError("O valor deve ser maior que zero.")
    if not (description or "").strip():
        raise ValueError("Informe a descrição da movimentação.")

    return db.execute(
        """INSERT INTO cash_movements
        (session_id,account,direction,category,amount_cents,description,source,source_id,
         created_by,reversed_movement_id)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            account,
            direction,
            category,
            int(amount_cents),
            description.strip(),
            source,
            source_id,
            created_by,
            reversed_movement_id,
        ),
    )


def session_summary(db, session):
    sales = db.execute(
        """SELECT
        COALESCE(SUM(CASE WHEN payment_method='Dinheiro' THEN total_cents ELSE 0 END),0) cash_sales,
        COALESCE(SUM(CASE WHEN payment_method IN ('Pix','Débito') THEN total_cents ELSE 0 END),0) bank_sales
        FROM sales
        WHERE paid=1 AND payment_method<>'Cortesia'
          AND date(COALESCE(paid_at,created_at))=?""",
        (session["business_date"],),
    ).fetchone()
    totals = db.execute(
        """SELECT
        COALESCE(SUM(CASE WHEN account='cash' AND direction='in' THEN amount_cents ELSE 0 END),0) cash_in,
        COALESCE(SUM(CASE WHEN account='cash' AND direction='out' THEN amount_cents ELSE 0 END),0) cash_out,
        COALESCE(SUM(CASE WHEN account='bank' AND direction='in' THEN amount_cents ELSE 0 END),0) bank_in,
        COALESCE(SUM(CASE WHEN account='bank' AND direction='out' THEN amount_cents ELSE 0 END),0) bank_out
        FROM cash_movements WHERE session_id=?""",
        (session["id"],),
    ).fetchone()

    calculated_cash = (
        session["opening_cash_cents"] + sales["cash_sales"] + totals["cash_in"] - totals["cash_out"]
    )
    calculated_bank = (
        session["opening_bank_cents"] + sales["bank_sales"] + totals["bank_in"] - totals["bank_out"]
    )
    closed = session["status"] == "closed"
    expected_cash = session["expected_cash_cents"] if closed else calculated_cash
    expected_bank = session["expected_bank_cents"] if closed else calculated_bank

    movements = db.execute(
        """SELECT m.*,u.name user_name,ct.reversed_at transfer_reversed,
        EXISTS(SELECT 1 FROM cash_movements r WHERE r.reversed_movement_id=m.id) reversed
        FROM cash_movements m LEFT JOIN users u ON u.id=m.created_by
        LEFT JOIN cash_transfers ct ON ct.id=m.source_id AND m.source IN ('transfer_out','transfer_in')
        WHERE m.session_id=? ORDER BY m.id DESC""",
        (session["id"],),
    ).fetchall()
    sale_rows = db.execute(
        """SELECT s.id,s.payment_method,s.total_cents,COALESCE(s.paid_at,s.created_at) payment_date,
        p.name player_name
        FROM sales s JOIN players p ON p.id=s.player_id
        WHERE s.paid=1 AND s.payment_method<>'Cortesia'
          AND date(COALESCE(s.paid_at,s.created_at))=?
        ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC""",
        (session["business_date"],),
    ).fetchall()
    return {
        "cash_sales": sales["cash_sales"],
        "bank_sales": sales["bank_sales"],
        "cash_in": totals["cash_in"],
        "cash_out": totals["cash_out"],
        "bank_in": totals["bank_in"],
        "bank_out": totals["bank_out"],
        "expected_cash": expected_cash,
        "expected_bank": expected_bank,
        "calculated_cash": calculated_cash,
        "calculated_bank": calculated_bank,
        "movements": movements,
        "sales": sale_rows,
        "changed_after_close": closed
        and (calculated_cash != expected_cash or calculated_bank != expected_bank),
    }


def history_rows(db, start_date, end_date, account="", direction="", category="", query=""):
    movement_conditions = ["s.business_date BETWEEN ? AND ?"]
    movement_params = [start_date, end_date]
    if account in ACCOUNT_LABELS:
        movement_conditions.append("m.account=?")
        movement_params.append(account)
    if direction in {"in", "out"}:
        movement_conditions.append("m.direction=?")
        movement_params.append(direction)
    if category in CATEGORY_LABELS:
        movement_conditions.append("m.category=?")
        movement_params.append(category)
    if query:
        movement_conditions.append("(LOWER(m.description) LIKE ? OR LOWER(COALESCE(u.name,'')) LIKE ?)")
        term = f"%{query.lower()}%"
        movement_params.extend([term, term])
    movements = db.execute(
        f"""SELECT m.*,s.business_date,u.name user_name,ct.reversed_at transfer_reversed,
        EXISTS(SELECT 1 FROM cash_movements r WHERE r.reversed_movement_id=m.id) reversed
        FROM cash_movements m JOIN cash_sessions s ON s.id=m.session_id
        LEFT JOIN users u ON u.id=m.created_by
        LEFT JOIN cash_transfers ct ON ct.id=m.source_id AND m.source IN ('transfer_out','transfer_in')
        WHERE {' AND '.join(movement_conditions)}
        ORDER BY s.business_date DESC,m.id DESC LIMIT 1000""",
        tuple(movement_params),
    ).fetchall()

    show_sales = direction != "out" and not category
    sale_rows = []
    if show_sales:
        sale_conditions = ["sl.paid=1", "sl.payment_method<>'Cortesia'", "date(COALESCE(sl.paid_at,sl.created_at)) BETWEEN ? AND ?"]
        sale_params = [start_date, end_date]
        if account == "cash":
            sale_conditions.append("sl.payment_method='Dinheiro'")
        elif account == "bank":
            sale_conditions.append("sl.payment_method IN ('Pix','Débito')")
        if query:
            sale_conditions.append("(LOWER(p.name) LIKE ? OR CAST(sl.id AS TEXT) LIKE ?)")
            term = f"%{query.lower()}%"
            sale_params.extend([term, term])
        sale_rows = db.execute(
            f"""SELECT sl.id,sl.payment_method,sl.total_cents,
            COALESCE(sl.paid_at,sl.created_at) payment_date,p.name player_name,
            date(COALESCE(sl.paid_at,sl.created_at)) business_date
            FROM sales sl JOIN players p ON p.id=sl.player_id
            WHERE {' AND '.join(sale_conditions)}
            ORDER BY COALESCE(sl.paid_at,sl.created_at) DESC,sl.id DESC LIMIT 1000""",
            tuple(sale_params),
        ).fetchall()

    sessions = db.execute(
        """SELECT s.*,op.name opened_by_name,cl.name closed_by_name
        FROM cash_sessions s LEFT JOIN users op ON op.id=s.opened_by
        LEFT JOIN users cl ON cl.id=s.closed_by
        WHERE s.business_date BETWEEN ? AND ? ORDER BY s.business_date DESC""",
        (start_date, end_date),
    ).fetchall()
    totals = {
        "sales": sum(int(row["total_cents"] or 0) for row in sale_rows),
        "in": sum(int(row["amount_cents"] or 0) for row in movements if row["direction"] == "in"),
        "out": sum(int(row["amount_cents"] or 0) for row in movements if row["direction"] == "out"),
        "cash_sales": sum(int(row["total_cents"] or 0) for row in sale_rows if row["payment_method"] == "Dinheiro"),
        "bank_sales": sum(int(row["total_cents"] or 0) for row in sale_rows if row["payment_method"] in ("Pix", "Débito")),
    }
    totals["net"] = totals["sales"] + totals["in"] - totals["out"]
    return {"movements": movements, "sales": sale_rows, "sessions": sessions, "totals": totals}
