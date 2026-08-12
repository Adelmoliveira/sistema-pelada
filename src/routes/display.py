from datetime import date

from flask import Blueprint, jsonify, render_template

from src.db import get_db
from src.routes.auth import roles_allowed
from src.routes.sales import delivery_order_data
from src.utils import local_today, service_medals


bp = Blueprint("display", __name__)


@bp.get("/painel")
@roles_allowed("manager", "staff", "display")
def panel():
    return render_template("display_panel.html")


@bp.get("/painel/feed")
@roles_allowed("manager", "staff", "display")
def feed():
    db = get_db()
    select = """SELECT s.*,COALESCE(p.name,s.guest_name,'Convidado') player_name,p.war_name,p.thumbnail_data player_thumbnail_data,
                       e.name event_name,u.name delivered_by_name
                FROM sales s LEFT JOIN players p ON p.id=s.player_id
                LEFT JOIN bar_events e ON e.id=s.event_id
                LEFT JOIN users u ON u.id=s.delivered_by"""
    pending_rows = db.execute(
        f"""{select} WHERE s.ready_for_delivery=1 AND s.delivered_at IS NULL
            AND (s.paid=1 OR s.payment_status='pending_cash')
            ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC"""
    ).fetchall()
    today = local_today()
    birthday_rows = db.execute(
        """SELECT name, war_name, gender, birth_date, thumbnail_data FROM players
           WHERE active=1 AND birth_date<>'' AND substr(birth_date,6,2)=?
           ORDER BY substr(birth_date,9,2), LOWER(COALESCE(war_name,name))""",
        (today.strftime("%m"),),
    ).fetchall()
    # Destaques: os peladeiros mais antigos com data de apresentação
    # cadastrada. Mantemos a consulta somente leitura para que o painel da TV
    # possa ser atualizado frequentemente sem alterar dados do grupo.
    highlight_rows = db.execute(
        """SELECT name, war_name, gender, football_join_date, thumbnail_data
           FROM players
           WHERE active=1 AND gender!='female' AND football_join_date<>''
           ORDER BY football_join_date ASC, LOWER(COALESCE(war_name,name))
           LIMIT 10"""
    ).fetchall()
    highlights = []
    for player in highlight_rows:
        raw_date = (player["football_join_date"] or "").strip()
        tenure_months = None
        try:
            joined = date.fromisoformat(
                raw_date + "-01" if len(raw_date) == 7 else raw_date[:10]
            )
            tenure_months = max(
                0,
                (today.year - joined.year) * 12
                + today.month - joined.month
                - (today.day < joined.day),
            )
        except (TypeError, ValueError):
            pass
        if tenure_months is None:
            tenure_label = "Tempo não informado"
        else:
            years, months = divmod(tenure_months, 12)
            parts = []
            if years:
                parts.append(f"{years} ano(s)")
            if months:
                parts.append(f"{months} mês(es)")
            tenure_label = " e ".join(parts) if parts else "menos de 1 mês"
        highlights.append({
            "name": player["name"],
            "war_name": player["war_name"],
            "thumbnail_data": player["thumbnail_data"],
            "tenure_label": tenure_label,
            "service_medals": service_medals(raw_date),
        })
    return jsonify(
        orders=[delivery_order_data(db, sale) for sale in pending_rows],
        birthdays=[dict(row) for row in birthday_rows],
        highlights=highlights,
        updated_at=today.isoformat(),
    )
