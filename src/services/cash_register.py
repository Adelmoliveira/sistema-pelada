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


def _sales_with_payment_breakdown(db, date_condition, date_params, query=""):
    conditions = ["s.paid=1", "s.payment_method<>'Cortesia'", date_condition]
    params = list(date_params)
    if query:
        conditions.append("(LOWER(COALESCE(p.name,s.guest_name,'')) LIKE ? OR CAST(s.id AS TEXT) LIKE ?)")
        term = f"%{query.lower()}%"
        params.extend([term, term])
    rows = db.execute(
        f"""SELECT s.id,s.payment_method,s.total_cents,
        COALESCE(s.paid_at,s.created_at) payment_date,
        COALESCE(p.name,s.guest_name,'Convidado') player_name,
        date(COALESCE(s.paid_at,s.created_at)) business_date,
        COALESCE(pp.part_count,0) payment_part_count,
        CASE WHEN COALESCE(pp.part_count,0)>0 THEN COALESCE(pp.cash_cents,0)
             WHEN s.payment_method='Dinheiro' THEN s.total_cents ELSE 0 END cash_cents,
        CASE WHEN COALESCE(pp.part_count,0)>0 THEN COALESCE(pp.pix_cents,0)
             WHEN s.payment_method IN ('Pix','Débito') THEN s.total_cents ELSE 0 END bank_cents,
        CASE WHEN COALESCE(pp.part_count,0)>0 THEN COALESCE(pp.credit_cents,0)
             WHEN s.payment_method='Créditos' THEN s.total_cents ELSE 0 END credit_cents
        FROM sales s LEFT JOIN players p ON p.id=s.player_id
        LEFT JOIN (
            SELECT sale_id,COUNT(*) part_count,
            COALESCE(SUM(CASE WHEN status='approved' AND method='Dinheiro' THEN amount_cents ELSE 0 END),0) cash_cents,
            COALESCE(SUM(CASE WHEN status='approved' AND method='Pix' THEN amount_cents ELSE 0 END),0) pix_cents,
            COALESCE(SUM(CASE WHEN status='approved' AND method='Créditos' THEN amount_cents ELSE 0 END),0) credit_cents
            FROM sale_payment_parts GROUP BY sale_id
        ) pp ON pp.sale_id=s.id
        WHERE {' AND '.join(conditions)}
        ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC""",
        tuple(params),
    ).fetchall()
    result = []
    for row in rows:
        sale = dict(row)
        breakdown = [
            ("Créditos", int(sale["credit_cents"] or 0), "Créditos do bar"),
            ("Pix", int(sale["bank_cents"] or 0), "Conta / Pix"),
            ("Dinheiro", int(sale["cash_cents"] or 0), "Dinheiro físico"),
        ]
        used = [entry for entry in breakdown if entry[1] > 0]
        sale["payment_label"] = " + ".join(entry[0] for entry in used) or sale["payment_method"]
        sale["account_label"] = " + ".join(entry[2] for entry in used) or "Sem entrada aprovada"
        result.append(sale)
    return result


def payment_breakdown(db, sale):
    parts = db.execute(
        """SELECT method,COALESCE(SUM(amount_cents),0) amount_cents
           FROM sale_payment_parts WHERE sale_id=? AND status='approved' GROUP BY method""",
        (sale["id"],),
    ).fetchall()
    has_parts = db.execute(
        "SELECT 1 FROM sale_payment_parts WHERE sale_id=? LIMIT 1", (sale["id"],)
    ).fetchone()
    amounts = {"Dinheiro": 0, "Pix": 0, "Créditos": 0}
    if has_parts:
        amounts.update({row["method"]: int(row["amount_cents"] or 0) for row in parts})
    elif sale["payment_method"] in amounts:
        amounts[sale["payment_method"]] = int(sale["total_cents"] or 0)
    return amounts


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
    sale_rows = _sales_with_payment_breakdown(
        db, "date(COALESCE(s.paid_at,s.created_at))=?", (session["business_date"],)
    )
    cash_sales = sum(row["cash_cents"] for row in sale_rows)
    bank_sales = sum(row["bank_cents"] for row in sale_rows)
    credit_sales = sum(row["credit_cents"] for row in sale_rows)
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
        session["opening_cash_cents"] + cash_sales + totals["cash_in"] - totals["cash_out"]
    )
    calculated_bank = (
        session["opening_bank_cents"] + bank_sales + totals["bank_in"] - totals["bank_out"]
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
    return {
        "cash_sales": cash_sales,
        "bank_sales": bank_sales,
        "credit_sales": credit_sales,
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
        sale_rows = _sales_with_payment_breakdown(
            db,
            "date(COALESCE(s.paid_at,s.created_at)) BETWEEN ? AND ?",
            (start_date, end_date),
            query,
        )
        if account == "cash":
            sale_rows = [row for row in sale_rows if row["cash_cents"] > 0]
        elif account == "bank":
            sale_rows = [row for row in sale_rows if row["bank_cents"] > 0]
        sale_rows = sale_rows[:1000]
        for row in sale_rows:
            row["display_amount_cents"] = (
                row["cash_cents"] if account == "cash"
                else row["bank_cents"] if account == "bank"
                else row["total_cents"]
            )

    sessions = db.execute(
        """SELECT s.*,op.name opened_by_name,cl.name closed_by_name
        FROM cash_sessions s LEFT JOIN users op ON op.id=s.opened_by
        LEFT JOIN users cl ON cl.id=s.closed_by
        WHERE s.business_date BETWEEN ? AND ? ORDER BY s.business_date DESC""",
        (start_date, end_date),
    ).fetchall()
    totals = {
        "sales": sum(
            int(row["cash_cents"] if account == "cash" else row["bank_cents"] if account == "bank" else row["total_cents"] or 0)
            for row in sale_rows
        ),
        "in": sum(int(row["amount_cents"] or 0) for row in movements if row["direction"] == "in"),
        "out": sum(int(row["amount_cents"] or 0) for row in movements if row["direction"] == "out"),
        "cash_sales": sum(int(row["cash_cents"] or 0) for row in sale_rows),
        "bank_sales": sum(int(row["bank_cents"] or 0) for row in sale_rows),
        "credit_sales": sum(int(row["credit_cents"] or 0) for row in sale_rows),
    }
    totals["net"] = totals["sales"] + totals["in"] - totals["out"]
    return {"movements": movements, "sales": sale_rows, "sessions": sessions, "totals": totals}
