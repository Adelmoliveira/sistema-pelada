from flask import Blueprint, jsonify, render_template

from src.db import get_db
from src.routes.auth import roles_allowed
from src.routes.sales import delivery_order_data
from src.utils import local_today


bp = Blueprint("display", __name__)


@bp.get("/painel")
@roles_allowed("manager", "staff", "display")
def panel():
    return render_template("display_panel.html")


@bp.get("/painel/feed")
@roles_allowed("manager", "staff", "display")
def feed():
    db = get_db()
    select = """SELECT s.*,p.name player_name,p.war_name,p.thumbnail_data player_thumbnail_data,
                       u.name delivered_by_name
                FROM sales s JOIN players p ON p.id=s.player_id
                LEFT JOIN users u ON u.id=s.delivered_by"""
    pending_rows = db.execute(
        f"""{select} WHERE s.ready_for_delivery=1 AND s.delivered_at IS NULL
            AND (s.paid=1 OR s.payment_status='pending_cash')
            ORDER BY COALESCE(s.paid_at,s.created_at),s.id"""
    ).fetchall()
    today = local_today()
    birthday_rows = db.execute(
        """SELECT name, war_name, gender, birth_date, thumbnail_data FROM players
           WHERE active=1 AND birth_date<>'' AND substr(birth_date,6,5)=?
           ORDER BY substr(birth_date,9,2), LOWER(COALESCE(war_name,name))""",
        (today.strftime("%m-%d"),),
    ).fetchall()
    return jsonify(
        orders=[delivery_order_data(db, sale) for sale in pending_rows],
        birthdays=[dict(row) for row in birthday_rows],
        updated_at=today.isoformat(),
    )
