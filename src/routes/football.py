from datetime import date
from html import escape

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import local_today, month_year_label, service_medals
from src.services.football_stats_pdf import build_football_stats_pdf
from src.services.football_tenure_pdf import build_football_tenure_pdf
from src.services.email_reminders import send_gmail_html
from src.services.push_notifications import send_player_push, send_player_push_once

bp = Blueprint("football", __name__, url_prefix="/futebol")

SITUATIONS = {"RASCUNHO": "Rascunho", "ABERTA": "Aberta", "EM_ANDAMENTO": "Em andamento", "FINALIZADA": "Finalizada", "CANCELADA": "Cancelada"}
PARTICIPANT_STATUSES = {"CONFIRMADO": "Confirmado", "AUSENTE": "Ausente", "DESISTENTE": "Desistente", "RESERVA": "Reserva"}
POSITIONS = {"GOLEIRO": "Goleiro", "DEFENSOR": "Defensor", "MEIO_CAMPO": "Meio-campo", "ATACANTE": "Atacante", "GOL": "Goleiro", "DEFESA": "Defesa", "MEIO": "Meio", "ATAQUE": "Ataque"}
PLAYER_POSITION_TO_LINEUP = {"GOL": "GOLEIRO", "DEFESA": "DEFENSOR", "MEIO": "MEIO_CAMPO", "ATAQUE": "ATACANTE"}
TEAMS = {"AZUL": "Azul", "BRANCO": "Branco"}
INCIDENT_TYPES = {"DISCIPLINAR": "Disciplinar", "LESAO": "Lesão", "ATRASO": "Atraso", "ABANDONO_PARTIDA": "Abandono de partida", "DISCUSSAO": "Discussão", "FALHA_ORGANIZACAO": "Falha de organização", "PROBLEMA_ESTRUTURAL": "Problema estrutural", "OUTRO": "Outro"}
INCIDENT_LEVELS = {"INFORMATIVO": "Informativo", "ATENCAO": "Atenção", "GRAVE": "Grave"}
CARD_TYPES = {"AMARELO": "Amarelo", "AZUL": "Azul", "VERMELHO": "Vermelho"}
TRANSFER_POSITIONS = {"DEFESA": "Defesa", "MEIO": "Meio", "ATAQUE": "Ataque"}
TRANSFER_STATUSES = {"PENDENTE": "Pendente", "APROVADA": "Deferido", "RECUSADA": "Indeferido"}


def _audit(db, sumula_id, action, details=""):
    db.execute("INSERT INTO football_audit(sumula_id,user_id,action,details) VALUES(?,?,?,?)", (sumula_id, g.user["id"], action, details))


def _eligible_player(db, player_id):
    return db.execute("SELECT id FROM players WHERE id=? AND active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO'", (player_id,)).fetchone()


def _participant_player(db, sumula_id, player_id):
    return db.execute("SELECT 1 FROM football_participants WHERE sumula_id=? AND player_id=?", (sumula_id, player_id)).fetchone()


def _fallback_roles(db, sumula_id):
    """Calcula os papéis de emergência da segunda partida pela ordem do sorteio."""
    match = db.execute("SELECT id FROM football_matches WHERE sumula_id=? AND number=2", (sumula_id,)).fetchone()
    if not match or db.execute(
        "SELECT 1 FROM football_responsibles WHERE sumula_id=? AND match_id=? AND responsibility_type='GOLEIRO_VOLUNTARIO'",
        (sumula_id, match["id"]),
    ).fetchone() or db.execute(
        "SELECT 1 FROM football_responsibles WHERE sumula_id=? AND SUBSTR(observation,1,17)='REGRA_AUTOMATICA_'",
        (sumula_id,),
    ).fetchone():
        return []
    players = {
        int(row["draw_order"]): row
        for row in db.execute(
            """SELECT fp.player_id,fp.draw_order,p.name,p.war_name
               FROM football_participants fp JOIN players p ON p.id=fp.player_id
               WHERE fp.sumula_id=? AND fp.status='CONFIRMADO' AND fp.draw_order IN (1,8,14,20)""",
            (sumula_id,),
        ).fetchall()
    }
    roles = []
    if players.get(1):
        player = players[1]
        roles.append({"player_id": player["player_id"], "match_id": match["id"], "role": "Goleiro", "draw_order": 1, "name": player["war_name"] or player["name"]})
    candidates = [players[order] for order in (8, 14, 20) if players.get(order)]
    if candidates:
        goalkeeper = candidates[0]
        roles.append({"player_id": goalkeeper["player_id"], "match_id": match["id"], "role": "Goleiro", "draw_order": goalkeeper["draw_order"], "name": goalkeeper["war_name"] or goalkeeper["name"]})
        for referee in candidates[1:]:
            roles.append({"player_id": referee["player_id"], "match_id": match["id"], "role": "Juiz", "draw_order": referee["draw_order"], "name": referee["war_name"] or referee["name"]})
    return roles


def _lineup_position(value):
    """Converte a posição do cadastro do peladeiro para a súmula."""
    normalized = (value or "").strip().upper()
    return PLAYER_POSITION_TO_LINEUP.get(normalized, normalized if normalized in POSITIONS else "")


def _ensure_goal_fits_score(db, match_id, team, exclude_goal_id=None):
    """Impede gols adicionais quando o placar encerrado já foi atingido."""
    match = db.execute("SELECT status,blue_score,white_score FROM football_matches WHERE id=?", (match_id,)).fetchone()
    if not match or match["status"] != "ENCERRADA":
        return
    score = int(match["blue_score"] or 0) if team == "AZUL" else int(match["white_score"] or 0)
    sql = "SELECT COUNT(*) FROM football_goals WHERE match_id=? AND benefited_team=?"
    params = [match_id, team]
    if exclude_goal_id:
        sql += " AND id!=?"
        params.append(exclude_goal_id)
    current = int(db.execute(sql, tuple(params)).fetchone()[0] or 0)
    if current >= score:
        raise ValueError(f"O placar do {team.title()} já atingiu {score} gol(s). Atualize o placar antes de registrar outro gol.")


def _score_mismatches(db, matches):
    mismatches = []
    for item in matches:
        match = item["row"]
        goals = db.execute(
            "SELECT benefited_team,COUNT(*) total FROM football_goals WHERE match_id=? GROUP BY benefited_team",
            (match["id"],),
        ).fetchall()
        counts = {row["benefited_team"]: int(row["total"]) for row in goals}
        if counts.get("AZUL", 0) != int(match["blue_score"] or 0) or counts.get("BRANCO", 0) != int(match["white_score"] or 0):
            mismatches.append(f"{match['number']}ª partida")
    return mismatches


def _matematico_results(db, sumula_id):
    """Retorna os placares quando todas as partidas encerradas têm diferença de até dois gols."""
    rows = db.execute(
        "SELECT number,blue_score,white_score,status FROM football_matches WHERE sumula_id=? ORDER BY number",
        (sumula_id,),
    ).fetchall()
    if not rows or any(row["status"] != "ENCERRADA" or abs(int(row["blue_score"] or 0) - int(row["white_score"] or 0)) > 2 for row in rows):
        return []
    return rows


def _match_day(value):
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError("Informe uma data válida para a pelada.")
    if parsed.weekday() not in (2, 5):
        raise ValueError("A data deve cair em uma quarta-feira ou sábado.")
    return parsed


def _transfer_window(today=None, db=None):
    """Retorna a janela anual, respeitando a abertura/fechamento manual do gerente."""
    today = today or local_today()
    setting = db.execute("SELECT is_open,manual_override,window_year FROM football_transfer_window_settings WHERE id=1").fetchone() if db is not None else None
    next_year = today.year if today.month < 2 else today.year + 1
    if setting and int(setting["manual_override"] or 0):
        return {"is_open": bool(setting["is_open"]), "year": int(setting["window_year"] or today.year), "next_date": date(next_year, 2, 1), "manual_override": True}
    is_open = today.month == 2
    return {"is_open": is_open, "year": today.year if is_open else next_year, "next_date": date(next_year, 2, 1), "manual_override": False}


def _transfer_metrics(db, player):
    total = int(db.execute("SELECT COUNT(*) FROM football_sumulas WHERE situacao='FINALIZADA'", ()).fetchone()[0] or 0)
    attended = int(db.execute("""SELECT COUNT(DISTINCT fp.sumula_id) FROM football_participants fp
        JOIN football_sumulas fs ON fs.id=fp.sumula_id
        WHERE fp.player_id=? AND fp.status='CONFIRMADO' AND fs.situacao='FINALIZADA'""", (player["id"],)).fetchone()[0] or 0)
    joined = (player["football_join_date"] or "").strip()
    tenure_months = None
    try:
        start = date.fromisoformat(joined + "-01" if len(joined) == 7 else joined[:10])
        today = local_today()
        tenure_months = max(0, (today.year - start.year) * 12 + today.month - start.month - (today.day < start.day))
    except (TypeError, ValueError):
        pass
    if tenure_months is None:
        tenure_label = "Não informado"
    else:
        years, months = divmod(tenure_months, 12)
        parts = []
        if years:
            parts.append(f"{years} ano" + ("s" if years != 1 else ""))
        if months:
            parts.append(f"{months} mês" + ("es" if months != 1 else ""))
        tenure_label = " e ".join(parts) if parts else "menos de 1 mês"
    frequency = round(attended / total * 100, 1) if total else 0
    return {"tenure_months": tenure_months, "tenure_label": tenure_label, "tenure_missing_months": None if tenure_months is None else max(0, 4 - tenure_months),
            "frequency": frequency, "frequency_missing_points": max(0, round(40 - frequency, 1)), "attended": attended, "total_sumulas": total}


def _transfer_analysis(db, player, requested_position):
    metrics = _transfer_metrics(db, player)
    counts = {key: int(db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND football_position=?", (key,)).fetchone()[0] or 0) for key in TRANSFER_POSITIONS}
    current = (player["football_position"] or "").strip().upper()
    projected = dict(counts)
    if current in projected:
        projected[current] = max(0, projected[current] - 1)
    if requested_position in projected:
        projected[requested_position] += 1
    total = sum(projected.values()) or 1
    targets = {"DEFESA": 30, "MEIO": 30, "ATAQUE": 40}
    impact = {key: {"current": round(counts[key] / total * 100, 1), "projected": round(projected[key] / total * 100, 1), "target": targets[key]} for key in TRANSFER_POSITIONS}
    max_deviation = max(abs(item["projected"] - item["target"]) for item in impact.values())
    reasons = []
    if metrics["tenure_months"] is None:
        reasons.append("não há data de apresentação cadastrada")
    elif metrics["tenure_months"] < 4:
        reasons.append("tempo de pelada inferior a 4 meses")
    if metrics["frequency"] < 40:
        reasons.append("frequência inferior a 40%")
    if max_deviation > 15:
        reasons.append("o impacto ultrapassa 15 pontos percentuais do equilíbrio 30/30/40")
    criteria = {
        "tenure_ok": metrics["tenure_months"] is not None and metrics["tenure_months"] >= 4,
        "frequency_ok": metrics["frequency"] >= 40,
        "balance_ok": max_deviation <= 15,
    }
    criteria["eligible"] = all(criteria.values())
    if not criteria["eligible"]:
        recommendation = "DESFAVORÁVEL"
        recommendation_reason = "Não atende aos critérios: " + "; ".join(reasons) + "."
    elif metrics["tenure_months"] < 12 or max_deviation > 8:
        recommendation = "ATENÇÃO"
        attention = []
        if metrics["tenure_months"] < 12:
            attention.append("tempo de pelada ainda inferior a 12 meses")
        if max_deviation > 8:
            attention.append("impacto acima de 8 pontos percentuais no equilíbrio")
        recommendation_reason = "Requer análise: " + "; ".join(attention) + "."
    else:
        recommendation = "FAVORÁVEL"
        recommendation_reason = "Atende às regras mínimas de tempo de pelada (4 meses ou mais), frequência (40% ou mais) e equilíbrio 30/30/40."
    return {"metrics": metrics, "impact": impact, "recommendation": recommendation, "recommendation_reason": recommendation_reason,
            "criteria": criteria, "max_deviation": round(max_deviation, 1)}


def _notify_transfer(current_app, recipient, subject, text, status="PENDENTE"):
    sender = current_app.config.get("GMAIL_SMTP_USER")
    password = current_app.config.get("GMAIL_APP_PASSWORD")
    if not recipient or not sender or not password:
        return
    try:
        colors = {"APROVADA": "#198754", "RECUSADA": "#dc3545", "PENDENTE": "#07558c"}
        labels = {"APROVADA": "Deferido", "RECUSADA": "Indeferido", "PENDENTE": "Em análise"}
        color = colors.get(status, colors["PENDENTE"])
        label = labels.get(status, labels["PENDENTE"])
        body = escape(text).replace("\n", "<br>")
        html = f"""<div style='margin:0;background:#f2f6f9;padding:24px;font-family:Arial,sans-serif;color:#183247'>
          <div style='max-width:620px;margin:auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 12px #1232'>
            <div style='background:#07558c;padding:20px;text-align:center'><img src='https://sistema-pelada-one.vercel.app/static/logo-gpcta.jpeg' alt='Logo GPCTA' style='max-width:110px;max-height:90px;object-fit:contain'><h1 style='color:#fff;font-size:22px;margin:10px 0 0'>PELADEIROS GPCTA</h1></div>
            <div style='padding:24px'><h2 style='margin-top:0;color:#07558c'>Solicitação de transferência</h2>
              <div style='display:inline-block;background:{color};color:#fff;border-radius:6px;padding:8px 14px;font-weight:bold;margin-bottom:18px'>Status: {label}</div>
              <div style='font-size:16px;line-height:1.55;color:#183042'>{body}</div>
              <p style='margin-top:24px;color:#607d8b;font-size:13px'>Mensagem enviada pelo sistema PELADEIROS GPCTA.</p>
            </div>
          </div>
        </div>"""
        send_gmail_html(sender, password, recipient, subject, text, html)
    except Exception as exc:
        current_app.logger.warning("Não foi possível enviar notificação de transferência: %s", exc)


def _transfer_rows(db, window_year):
    rows = db.execute("""SELECT tr.*,p.name,p.war_name,p.football_position,p.football_join_date
        FROM football_transfer_requests tr JOIN players p ON p.id=tr.player_id
        WHERE tr.window_year=? ORDER BY CASE tr.status WHEN 'PENDENTE' THEN 0 ELSE 1 END,tr.created_at DESC,tr.id DESC""", (window_year,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        analysis = _transfer_analysis(db, row, row["requested_position"])
        item["metrics"] = analysis["metrics"]
        item["impact"] = analysis["impact"]
        item["recommendation"] = analysis["recommendation"]
        result.append(item)
    return result


def _sumula(db, sumula_id, audit_page=None):
    row = db.execute("SELECT fs.*,u.name created_by_name FROM football_sumulas fs LEFT JOIN users u ON u.id=fs.created_by WHERE fs.id=?", (sumula_id,)).fetchone()
    if not row:
        return None
    participants = db.execute("SELECT fp.*,p.name,p.war_name,p.photo_data,p.thumbnail_data,p.football_position FROM football_participants fp JOIN players p ON p.id=fp.player_id WHERE fp.sumula_id=? ORDER BY COALESCE(fp.draw_order,999999),LOWER(p.war_name),LOWER(p.name)", (sumula_id,)).fetchall()
    matches = []
    for match in db.execute("SELECT * FROM football_matches WHERE sumula_id=? ORDER BY number", (sumula_id,)).fetchall():
        lineups = [dict(item) for item in db.execute("SELECT fl.*,p.name,p.war_name FROM football_lineups fl JOIN players p ON p.id=fl.player_id WHERE fl.match_id=? ORDER BY fl.period,fl.team,fl.position,COALESCE(fl.draw_order,999999),LOWER(p.name)", (match["id"],)).fetchall()]
        for lineup in lineups:
            lineup["war_name"] = f"{lineup['war_name'] or lineup['name']} · Tempo {lineup['period']}"
        goals = db.execute("SELECT fg.*,pa.name author_name,pa.war_name author_war,ps.name assist_name,ps.war_name assist_war FROM football_goals fg LEFT JOIN players pa ON pa.id=fg.author_player_id LEFT JOIN players ps ON ps.id=fg.assist_player_id WHERE fg.match_id=? ORDER BY fg.id", (match["id"],)).fetchall()
        matches.append({"row": match, "lineups": lineups, "goals": goals})
    incidents = db.execute("SELECT fi.*,p.name,p.war_name FROM football_incidents fi LEFT JOIN players p ON p.id=fi.player_id WHERE fi.sumula_id=? ORDER BY fi.id DESC", (sumula_id,)).fetchall()
    responsibles = db.execute("SELECT fr.*,p.name,p.war_name,fm.number match_number FROM football_responsibles fr LEFT JOIN players p ON p.id=fr.player_id LEFT JOIN football_matches fm ON fm.id=fr.match_id WHERE fr.sumula_id=? ORDER BY fr.id", (sumula_id,)).fetchall()
    if audit_page is None:
        audits = db.execute("SELECT fa.*,u.name user_name FROM football_audit fa LEFT JOIN users u ON u.id=fa.user_id WHERE fa.sumula_id=? ORDER BY fa.id DESC LIMIT 30", (sumula_id,)).fetchall()
    else:
        audits = db.execute("SELECT fa.*,u.name user_name FROM football_audit fa LEFT JOIN users u ON u.id=fa.user_id WHERE fa.sumula_id=? ORDER BY fa.id DESC LIMIT 5 OFFSET ?", (sumula_id, max(0, audit_page - 1) * 5)).fetchall()
    return row, participants, matches, incidents, responsibles, audits


def _position_distribution(db):
    eligible_players = db.execute(
        "SELECT id,name,war_name,football_position FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO' ORDER BY LOWER(COALESCE(war_name,name))"
    ).fetchall()
    distribution = {"ATAQUE": 0, "MEIO": 0, "DEFESA": 0, "SEM_POSICAO": 0}
    position_players = {key: [] for key in distribution}
    counted_players = 0
    for player in eligible_players:
        position = (player["football_position"] or "").strip().upper()
        if position in ("GOL", "JUIZ", "APOSENTADO"):
            continue
        counted_players += 1
        category = position if position in ("ATAQUE", "MEIO") else "DEFESA" if position == "DEFESA" else "SEM_POSICAO"
        distribution[category] += 1
        position_players[category].append(player)
    positioned_total = sum(distribution[key] for key in ("ATAQUE", "MEIO", "DEFESA"))
    position_summary = []
    for key, label, target in (("ATAQUE", "Ataque", 40), ("MEIO", "Meio", 30), ("DEFESA", "Defesa", 30)):
        count = distribution[key]
        percentage = round((count / positioned_total) * 100, 2) if positioned_total else 0
        position_summary.append({"key": key, "label": label, "count": count, "percentage": percentage, "target": target, "difference": round(percentage - target, 2)})
    return position_summary, counted_players, positioned_total


@bp.get("")
@roles_allowed("manager", "football_manager")
def dashboard():
    db = get_db()
    today = local_today()
    month_start = today.replace(day=1)
    month_end = date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)
    year_start = date(today.year, 1, 1)
    year_end = date(today.year + 1, 1, 1)
    metrics = db.execute("SELECT COUNT(*) total,COUNT(CASE WHEN situacao='FINALIZADA' THEN 1 END) finalized,COUNT(CASE WHEN situacao IN ('ABERTA','EM_ANDAMENTO') THEN 1 END) active,COUNT(CASE WHEN match_date>=? AND situacao!='CANCELADA' THEN 1 END) upcoming FROM football_sumulas", (today.isoformat(),)).fetchone()
    recent = db.execute("SELECT * FROM football_sumulas WHERE situacao!='CANCELADA' ORDER BY match_date DESC,id DESC LIMIT 8").fetchall()
    wins = {}
    for label, start, end in (("Mês atual", month_start, month_end), ("Ano atual", year_start, year_end)):
        row = db.execute("SELECT COUNT(CASE WHEN fm.blue_score > fm.white_score THEN 1 END) blue_wins, COUNT(CASE WHEN fm.white_score > fm.blue_score THEN 1 END) white_wins FROM football_matches fm JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fm.status='ENCERRADA' AND fs.situacao!='CANCELADA' AND fs.match_date>=? AND fs.match_date<?", (start.isoformat(), end.isoformat())).fetchone()
        wins[label] = {"azul": int(row["blue_wins"] or 0), "branco": int(row["white_wins"] or 0)}
    position_summary, eligible_total, positioned_total = _position_distribution(db)
    return render_template("football_dashboard.html", metrics=metrics, recent=recent, situations=SITUATIONS, position_summary=position_summary, eligible_total=eligible_total, positioned_total=positioned_total, team_wins=wins, management_view=True)


@bp.get("/gestao/posicoes")
@roles_allowed("manager", "football_manager")
def position_distribution():
    db = get_db()
    position_summary, eligible_total, positioned_total = _position_distribution(db)
    return render_template("football_dashboard.html", metrics=None, recent=[], situations=SITUATIONS, position_summary=position_summary, eligible_total=eligible_total, positioned_total=positioned_total, management_view=True)


@bp.get("/gestao/tempo-futebol")
@roles_allowed("manager", "football_manager")
def tenure_report():
    """Lista os peladeiros pelo tempo desde a apresentação no grupo."""
    db = get_db()
    today = local_today()
    cadastro_filter = request.args.get("cadastro", "todos").strip().lower()
    if cadastro_filter not in {"todos", "cadastrados", "nao_cadastrados"}:
        cadastro_filter = "todos"
    players = db.execute(
        """SELECT id,name,war_name,football_join_date,football_position,membership_type
           FROM players
           WHERE active=1 AND gender!='female'
           ORDER BY LOWER(COALESCE(war_name,name)),LOWER(name)"""
    ).fetchall()
    rows = []
    for player in players:
        raw_date = (player["football_join_date"] or "").strip()
        years = months = None
        if raw_date:
            try:
                joined = date.fromisoformat(raw_date + "-01" if len(raw_date) == 7 else raw_date[:10])
                months = max(0, (today.year - joined.year) * 12 + today.month - joined.month - (today.day < joined.day))
                years, remaining_months = divmod(months, 12)
                tenure_label = f"{years} ano(s)" if not remaining_months else f"{years} ano(s) e {remaining_months} mês(es)"
            except ValueError:
                raw_date = ""
        if not raw_date:
            tenure_label = "Não informada"
        rows.append({
            "id": player["id"], "name": player["name"], "war_name": player["war_name"],
            "football_join_date": raw_date, "football_position": player["football_position"],
            "membership_type": player["membership_type"],
            "months": months, "tenure_label": tenure_label,
            "service_medals": service_medals(raw_date),
        })
    if cadastro_filter == "cadastrados":
        rows = [row for row in rows if row["football_join_date"]]
    elif cadastro_filter == "nao_cadastrados":
        rows = [row for row in rows if not row["football_join_date"]]
    rows.sort(key=lambda item: (item["months"] is None, -(item["months"] or 0), (item["war_name"] or item["name"]).lower()))
    for row in rows:
        row["position_label"] = {"GOL": "Goleiro", "DEFESA": "Defesa", "MEIO": "Meio", "ATAQUE": "Ataque", "APOSENTADO": "Aposentado"}.get(row["football_position"], "Não definida")
        row["join_date_label"] = month_year_label(row["football_join_date"])
    if request.args.get("pdf") == "1":
        return send_file(build_football_tenure_pdf(rows, today), mimetype="application/pdf", as_attachment=False, download_name="tempo-de-futebol.pdf")
    return render_template("football_tenure.html", rows=rows, today=today, cadastro_filter=cadastro_filter)


@bp.get("/estatisticas")
@roles_allowed("manager", "football_manager")
def statistics():
    db = get_db()
    year = request.args.get("year", "").strip()
    month = request.args.get("month", "").strip()
    player_id = request.args.get("player_id", "").strip()
    try:
        year_int = int(year) if year else None
        month_int = int(month) if month else None
        if year_int and not 2000 <= year_int <= 2100: raise ValueError
        if month_int and not 1 <= month_int <= 12: raise ValueError
        player_int = int(player_id) if player_id else None
    except ValueError:
        year = month = player_id = ""
        year_int = month_int = player_int = None
    start_date = end_date = None
    if year_int:
        start_date = date(year_int, month_int or 1, 1)
        if month_int:
            end_date = date(year_int + (1 if month_int == 12 else 0), 1 if month_int == 12 else month_int + 1, 1)
        else:
            end_date = date(year_int + 1, 1, 1)
    fs_filter = ""
    fs_params = []
    if start_date:
        fs_filter += " AND fs.match_date >= ? AND fs.match_date < ?"
        fs_params.extend((start_date.isoformat(), end_date.isoformat()))
    totals = db.execute(f"SELECT COUNT(DISTINCT fs.id) sumulas,COUNT(DISTINCT fm.id) partidas,COUNT(DISTINCT fg.id) gols FROM football_sumulas fs LEFT JOIN football_matches fm ON fm.sumula_id=fs.id AND fm.status='ENCERRADA' LEFT JOIN football_goals fg ON fg.match_id=fm.id WHERE fs.situacao='FINALIZADA'{fs_filter}", tuple(fs_params)).fetchone()
    finalized_sumulas = int(totals["sumulas"] or 0)
    player_stats = []
    player_where = "WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO'"
    player_params = []
    if player_int:
        player_where += " AND id=?"; player_params.append(player_int)
    for player in db.execute(f"SELECT id,name,war_name FROM players {player_where} ORDER BY LOWER(name)", tuple(player_params)).fetchall():
        participacoes = int(db.execute(f"SELECT COUNT(DISTINCT fp.sumula_id) FROM football_participants fp JOIN football_sumulas fs ON fs.id=fp.sumula_id WHERE fp.player_id=? AND fp.status='CONFIRMADO' AND fs.situacao='FINALIZADA'{fs_filter}", (player["id"], *fs_params)).fetchone()[0] or 0)
        games = db.execute("""SELECT DISTINCT fl.match_id,fl.team,fm.blue_score,fm.white_score FROM football_lineups fl
            JOIN football_matches fm ON fm.id=fl.match_id AND fm.status='ENCERRADA'
            JOIN football_sumulas fs ON fs.id=fm.sumula_id AND fs.situacao='FINALIZADA'
            WHERE fl.player_id=?""" + fs_filter, (player["id"], *fs_params)).fetchall()
        wins = draws = losses = 0
        for game in games:
            own, opponent = (int(game["blue_score"] or 0), int(game["white_score"] or 0)) if game["team"] == "AZUL" else (int(game["white_score"] or 0), int(game["blue_score"] or 0))
            if own > opponent: wins += 1
            elif own == opponent: draws += 1
            else: losses += 1
        goals = int(db.execute("SELECT COUNT(*) FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fg.author_player_id=? AND fm.status='ENCERRADA' AND fs.situacao='FINALIZADA'" + fs_filter, (player["id"], *fs_params)).fetchone()[0] or 0)
        assists = int(db.execute("SELECT COUNT(*) FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fg.assist_player_id=? AND fm.status='ENCERRADA' AND fs.situacao='FINALIZADA'" + fs_filter, (player["id"], *fs_params)).fetchone()[0] or 0)
        historical_filter = " AND stat_date >= ? AND stat_date < ?" if start_date else ""
        historical_params = (start_date.isoformat(), end_date.isoformat()) if start_date else ()
        historical = db.execute("SELECT COALESCE(SUM(goals),0) goals,COALESCE(SUM(assists),0) assists FROM football_historical_stats WHERE player_id=?" + historical_filter, (player["id"], *historical_params)).fetchone()
        goals += int(historical["goals"] or 0); assists += int(historical["assists"] or 0)
        if participacoes or games or goals or assists:
            player_stats.append({"id": player["id"], "name": player["name"], "war_name": player["war_name"], "participacoes": participacoes, "frequencia": round((participacoes / finalized_sumulas) * 100, 1) if finalized_sumulas else 0, "jogos": len(games), "vitorias": wins, "empates": draws, "derrotas": losses, "gols": goals, "assistencias": assists})
    player_stats.sort(key=lambda item: (-item["gols"], -item["assistencias"], -item["vitorias"], -item["participacoes"], (item["war_name"] or item["name"]).lower()))
    team_results = db.execute("""SELECT fm.*,fs.match_date FROM football_matches fm JOIN football_sumulas fs ON fs.id=fm.sumula_id
        WHERE fm.status='ENCERRADA'""" + fs_filter + " ORDER BY fs.match_date DESC,fm.number DESC LIMIT 20", tuple(fs_params)).fetchall()
    players = db.execute("SELECT id,name,war_name FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO' ORDER BY LOWER(COALESCE(war_name,name)),LOWER(name)").fetchall()
    selected_player = next((item for item in players if str(item["id"]) == player_id), None)
    filters = {"year": year, "month": month, "player_id": player_id, "player_name": (selected_player["war_name"] or selected_player["name"]) if selected_player else ""}
    if request.args.get("pdf") == "1":
        report = build_football_stats_pdf(player_stats, totals, filters, local_today())
        return send_file(report, mimetype="application/pdf", as_attachment=False, download_name="estatisticas-futebol.pdf")
    return render_template("football_statistics.html", totals=totals, player_stats=player_stats, team_results=team_results, players=players, filters=filters)


@bp.get("/frequencia")
@roles_allowed("manager", "football_manager")
def attendance():
    db = get_db()
    total_sumulas = int(db.execute(
        "SELECT COUNT(*) FROM football_sumulas WHERE situacao='FINALIZADA'"
    ).fetchone()[0] or 0)
    players = db.execute(
        """SELECT p.id,p.name,p.war_name,p.football_position,
                  COUNT(DISTINCT CASE WHEN fp.status='CONFIRMADO' AND fs.id IS NOT NULL THEN fp.sumula_id END) participacoes
           FROM players p
           LEFT JOIN football_participants fp ON fp.player_id=p.id
           LEFT JOIN football_sumulas fs ON fs.id=fp.sumula_id AND fs.situacao='FINALIZADA'
           WHERE p.active=1 AND p.gender!='female' AND p.membership_type!='veteran' AND COALESCE(p.football_position,'')!='APOSENTADO'
           GROUP BY p.id,p.name,p.war_name,p.football_position
           ORDER BY participacoes DESC,LOWER(COALESCE(p.war_name,p.name)),LOWER(p.name)"""
    ).fetchall()
    rows = []
    for player in players:
        participacoes = int(player["participacoes"] or 0)
        rows.append({
            "id": player["id"],
            "name": player["name"],
            "war_name": player["war_name"],
            "football_position": player["football_position"],
            "participacoes": participacoes,
            "ausencias": max(0, total_sumulas - participacoes),
            "frequencia": round((participacoes / total_sumulas) * 100, 2) if total_sumulas else 0,
        })
    return render_template("football_attendance.html", rows=rows, total_sumulas=total_sumulas)


@bp.route("/lancamentos", methods=["GET", "POST"])
@roles_allowed("manager", "football_manager")
def historical_stats():
    db = get_db()
    if request.method == "POST":
        try:
            player_id = int(request.form["player_id"])
            stat_date = date.fromisoformat(request.form.get("stat_date", "").strip()).isoformat()
            goals = max(0, int(request.form.get("goals", "0") or 0))
            assists = max(0, int(request.form.get("assists", "0") or 0))
            notes = request.form.get("notes", "").strip()[:500]
            if not db.execute("SELECT 1 FROM players WHERE id=? AND active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO'", (player_id,)).fetchone(): raise ValueError("Selecione um peladeiro válido.")
            if goals == 0 and assists == 0: raise ValueError("Informe pelo menos um gol ou uma assistência.")
            db.execute("INSERT INTO football_historical_stats(player_id,stat_date,goals,assists,notes,created_by) VALUES(?,?,?,?,?,?)", (player_id, stat_date, goals, assists, notes, g.user["id"]))
            db.commit(); flash("Lançamento histórico registrado.", "success")
        except (ValueError, KeyError):
            db.rollback(); flash("Informe peladeiro, data e pelo menos um gol ou assistência válidos.", "danger")
        return redirect(url_for("football.historical_stats"))
    players = db.execute("SELECT id,name,war_name FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO' ORDER BY LOWER(COALESCE(war_name,name))").fetchall()
    rows = db.execute("SELECT hs.*,p.name,p.war_name FROM football_historical_stats hs JOIN players p ON p.id=hs.player_id ORDER BY hs.stat_date DESC,hs.id DESC").fetchall()
    return render_template("football_historical_stats.html", players=players, rows=rows, today=local_today().isoformat())


@bp.get("/minha-pelada")
@roles_allowed("client")
def client_panel():
    db = get_db()
    player_id = g.user["player_id"]
    today = local_today().isoformat()
    sumula = db.execute("SELECT * FROM football_sumulas WHERE situacao!='CANCELADA' AND match_date>=? ORDER BY match_date,id LIMIT 1", (today,)).fetchone()
    if not sumula:
        sumula = db.execute("SELECT * FROM football_sumulas WHERE situacao!='CANCELADA' ORDER BY match_date DESC,id DESC LIMIT 1").fetchone()
    data = _sumula(db, sumula["id"]) if sumula else None
    if data:
        # A súmula pode criar partidas futuras como placeholders. Na visão do
        # peladeiro, exiba somente partidas efetivamente utilizadas (ou
        # encerradas), preservando inclusive partidas reais com placar 0 x 0.
        used_matches = [
            item for item in data[2]
            if item["row"]["status"] == "ENCERRADA"
            or int(item["row"]["blue_score"] or 0) != 0
            or int(item["row"]["white_score"] or 0) != 0
            or item["lineups"]
            or item["goals"]
        ]
        data = (data[0], data[1], used_matches, data[3], data[4], data[5])
    own = {"participacoes": 0, "jogos": 0, "vitorias": 0, "empates": 0, "derrotas": 0, "gols": 0, "assistencias": 0}
    if player_id:
        own["participacoes"] = int(db.execute("SELECT COUNT(DISTINCT sumula_id) FROM football_participants WHERE player_id=? AND status='CONFIRMADO' AND sumula_id IN (SELECT id FROM football_sumulas WHERE situacao='FINALIZADA')", (player_id,)).fetchone()[0] or 0)
        games = db.execute("""SELECT DISTINCT fl.match_id,fl.team,fm.blue_score,fm.white_score FROM football_lineups fl JOIN football_matches fm ON fm.id=fl.match_id AND fm.status='ENCERRADA' JOIN football_sumulas fs ON fs.id=fm.sumula_id AND fs.situacao='FINALIZADA' WHERE fl.player_id=?""", (player_id,)).fetchall()
        own["jogos"] = len(games)
        for game in games:
            score = (int(game["blue_score"] or 0), int(game["white_score"] or 0)) if game["team"] == "AZUL" else (int(game["white_score"] or 0), int(game["blue_score"] or 0))
            if score[0] > score[1]: own["vitorias"] += 1
            elif score[0] == score[1]: own["empates"] += 1
            else: own["derrotas"] += 1
        own["gols"] = int(db.execute("SELECT COUNT(*) FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fg.author_player_id=? AND fm.status='ENCERRADA' AND fs.situacao='FINALIZADA'", (player_id,)).fetchone()[0] or 0)
        own["assistencias"] = int(db.execute("SELECT COUNT(*) FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fg.assist_player_id=? AND fm.status='ENCERRADA' AND fs.situacao='FINALIZADA'", (player_id,)).fetchone()[0] or 0)
        historical = db.execute("SELECT COALESCE(SUM(goals),0) goals,COALESCE(SUM(assists),0) assists FROM football_historical_stats WHERE player_id=?", (player_id,)).fetchone()
        own["gols"] += int(historical["goals"] or 0); own["assistencias"] += int(historical["assists"] or 0)
    return render_template("football_client_panel.html", data=data, own=own, player_id=player_id)


@bp.route("/transferencia", methods=["GET", "POST"])
@roles_allowed("client", "manager", "football_manager")
def transfer_window():
    db = get_db()
    window = _transfer_window(db=db)
    is_manager = g.user["role"] in ("manager", "football_manager")
    if request.method == "POST":
        if not is_manager:
            if not window["is_open"]:
                flash(f"A janela de transferência abre em fevereiro de {window['year']}.", "warning")
            else:
                try:
                    player = db.execute("SELECT * FROM players WHERE id=? AND active=1", (g.user["player_id"],)).fetchone()
                    requested = request.form.get("requested_position", "").strip().upper()
                    reason = request.form.get("reason", "").strip()[:500]
                    if not player or requested not in TRANSFER_POSITIONS:
                        raise ValueError("Selecione uma posição válida.")
                    current = (player["football_position"] or "").strip().upper()
                    if current not in TRANSFER_POSITIONS:
                        raise ValueError("Sua posição atual precisa estar cadastrada antes de solicitar a transferência.")
                    if requested == current:
                        raise ValueError("A nova posição deve ser diferente da posição atual.")
                    existing = db.execute("SELECT id FROM football_transfer_requests WHERE player_id=? AND window_year=? AND status='PENDENTE'", (player["id"], window["year"])).fetchone()
                    if existing:
                        raise ValueError("Você já possui uma solicitação nesta janela.")
                    db.execute("""INSERT INTO football_transfer_requests(player_id,window_year,current_position,requested_position,reason)
                        VALUES(?,?,?,?,?)""", (player["id"], window["year"], current, requested, reason))
                    db.commit()
                    send_player_push(db, player["id"], "Janela de transferência aberta", f"A janela de transferência de {window['year']} está aberta. Sua solicitação foi enviada para avaliação.", "/notificacoes")
                    _notify_transfer(current_app, current_app.config.get("GMAIL_SMTP_USER"), "Nova solicitação de transferência", f"Nova solicitação de {player['war_name'] or player['name']} para mudar de {TRANSFER_POSITIONS[current]} para {TRANSFER_POSITIONS[requested]}.")
                    flash("Solicitação de transferência enviada para avaliação.", "success")
                except ValueError as exc:
                    db.rollback(); flash(str(exc), "danger")
                except Exception as exc:
                    db.rollback(); current_app.logger.error(f"Erro ao solicitar transferência: {exc}")
                    flash("Não foi possível registrar a solicitação.", "danger")
        else:
            try:
                window_action = request.form.get("action", "")
                if window_action in ("open_window", "close_window", "calendar_window"):
                    if window_action == "calendar_window":
                        db.execute("DELETE FROM football_transfer_window_settings WHERE id=1")
                        message = "A janela voltou a seguir o calendário automático de fevereiro."
                    else:
                        is_open = 1 if window_action == "open_window" else 0
                        db.execute("""INSERT INTO football_transfer_window_settings(id,is_open,manual_override,window_year,updated_by)
                            VALUES(1,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET is_open=excluded.is_open,manual_override=1,window_year=excluded.window_year,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP""",
                            (is_open, 1, local_today().year, g.user["id"]))
                        message = "Janela de transferência aberta." if is_open else "Janela de transferência fechada."
                    db.commit()
                    flash(message, "success")
                    return redirect(url_for("football.transfer_window"))
                if request.form.get("action") == "join_date":
                    player_id = int(request.form["player_id"])
                    join_date = request.form.get("football_join_date", "").strip()
                    if join_date:
                        join_date = date.fromisoformat(join_date + "-01" if len(join_date) == 7 else join_date).isoformat()
                    db.execute("UPDATE players SET football_join_date=? WHERE id=? AND active=1", (join_date, player_id))
                    db.commit(); flash("Data de apresentação atualizada.", "success")
                    return redirect(url_for("football.transfer_window"))
                request_id = int(request.form["request_id"])
                decision = request.form.get("decision", "").upper()
                notes = request.form.get("review_notes", "").strip()[:500]
                if decision not in ("APROVADA", "RECUSADA"):
                    raise ValueError("Decisão inválida.")
                item = db.execute("SELECT * FROM football_transfer_requests WHERE id=? AND status='PENDENTE'", (request_id,)).fetchone()
                if not item:
                    raise ValueError("Solicitação não encontrada ou já avaliada.")
                if decision == "APROVADA":
                    db.execute("UPDATE players SET football_position=? WHERE id=? AND active=1", (item["requested_position"], item["player_id"]))
                db.execute("""UPDATE football_transfer_requests SET status=?,reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP,review_notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (decision, g.user["id"], notes, request_id))
                db.commit()
                player = db.execute("SELECT name,war_name,email FROM players WHERE id=?", (item["player_id"],)).fetchone()
                if player and player["email"]:
                    message = f"Olá, {player['war_name'] or player['name']}!\n\nSua solicitação de transferência de posição foi {'deferida' if decision == 'APROVADA' else 'indeferida'}."
                    if notes:
                        message += f"\n\nMotivo da diretoria: {notes}"
                    _notify_transfer(current_app, player["email"], "Resultado da solicitação de transferência", message, decision)
                if player:
                    send_player_push(db, item["player_id"], "Transferência deferida" if decision == "APROVADA" else "Transferência indeferida", "Sua solicitação de mudança de posição foi deferida." if decision == "APROVADA" else "Sua solicitação de mudança de posição foi indeferida.", "/notificacoes")
                flash("Solicitação deferida e posição atualizada." if decision == "APROVADA" else "Solicitação indeferida.", "success")
            except (ValueError, KeyError) as exc:
                db.rollback(); flash(str(exc), "danger")
            except Exception as exc:
                db.rollback(); current_app.logger.error(f"Erro ao avaliar transferência: {exc}")
                flash("Não foi possível avaliar a solicitação.", "danger")
        return redirect(url_for("football.transfer_window"))
    player = None
    own_request = None
    own_history = []
    if not is_manager:
        player = db.execute("SELECT * FROM players WHERE id=? AND active=1", (g.user["player_id"],)).fetchone()
        if player:
            own_request = db.execute("SELECT * FROM football_transfer_requests WHERE player_id=? ORDER BY window_year DESC,id DESC LIMIT 1", (player["id"],)).fetchone()
            own_history = db.execute("SELECT * FROM football_transfer_requests WHERE player_id=? ORDER BY window_year DESC,id DESC", (player["id"],)).fetchall()
    player_analysis = []
    if player and (player["football_position"] or "").upper() in TRANSFER_POSITIONS:
        current_position = (player["football_position"] or "").upper()
        for requested_position in TRANSFER_POSITIONS:
            if requested_position != current_position:
                analysis = _transfer_analysis(db, player, requested_position)
                analysis["requested_position"] = requested_position
                player_analysis.append(analysis)
    rows = _transfer_rows(db, window["year"]) if is_manager else []
    position_counts = {key: int(db.execute("SELECT COUNT(*) FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND football_position=?", (key,)).fetchone()[0] or 0) for key in TRANSFER_POSITIONS}
    return render_template("football_transfer.html", window=window, is_manager=is_manager, player=player, own_request=own_request, own_history=own_history, player_analysis=player_analysis, rows=rows, transfer_positions=TRANSFER_POSITIONS, transfer_statuses=TRANSFER_STATUSES, position_counts=position_counts)


@bp.route("/notificacoes", methods=["GET", "POST"])
@roles_allowed("manager", "football_manager", "client")
def notifications():
    if g.user["role"] == "client":
        return redirect(url_for("auth.notifications_inbox"))
    db = get_db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:80]
        body = request.form.get("body", "").strip()[:500]
        audience = request.form.get("audience", "all")
        if audience not in ("all", "football"):
            audience = "all"
        try:
            if not title or not body:
                raise ValueError("Informe o título e a mensagem do aviso.")
            clause = "active=1" if audience == "all" else "active=1 AND gender!='female' AND membership_type!='veteran'"
            recipients = db.execute(f"SELECT id FROM players WHERE {clause}").fetchall()
            sent = 0
            for player in recipients:
                    sent += int(send_player_push(db, player["id"], title, body, "/notificacoes").get("sent", 0))
            db.execute("INSERT INTO push_announcements(title,body,audience,sent_count,created_by) VALUES(?,?,?,?,?)", (title, body, audience, sent, g.user["id"]))
            db.commit()
            flash(f"Aviso enviado para {sent} dispositivo(s) inscrito(s).", "success")
        except ValueError as exc:
            db.rollback(); flash(str(exc), "danger")
        except Exception as exc:
            db.rollback(); current_app.logger.error("Erro ao enviar aviso push: %s", exc)
            flash("Não foi possível enviar o aviso.", "danger")
        return redirect(url_for("football.notifications"))
    history = db.execute("""SELECT pa.*,u.name user_name FROM push_announcements pa
        LEFT JOIN users u ON u.id=pa.created_by ORDER BY pa.id DESC LIMIT 50""").fetchall()
    return render_template("football_notifications.html", history=history)


@bp.post("/notificacoes/historico/limpar")
@roles_allowed("manager", "football_manager")
def clear_notification_history():
    db = get_db()
    db.execute("DELETE FROM push_announcements")
    db.commit()
    flash("Histórico de avisos apagado.", "success")
    return redirect(url_for("football.notifications"))


@bp.get("/sumulas")
@roles_allowed("manager", "football_manager")
def sumulas():
    db = get_db()
    conditions, params = [], []
    start, end, situation = request.args.get("start", ""), request.args.get("end", ""), request.args.get("situacao", "")
    if start:
        conditions.append("fs.match_date>=?"); params.append(start)
    if end:
        conditions.append("fs.match_date<=?"); params.append(end)
    if situation == "ENCERRADA":
        conditions.append("fs.locked_at IS NOT NULL")
    elif situation in SITUATIONS:
        conditions.append("fs.situacao=?"); params.append(situation)
    sql = "SELECT fs.*,COUNT(DISTINCT fp.player_id) participant_count,COUNT(DISTINCT fm.id) match_count FROM football_sumulas fs LEFT JOIN football_participants fp ON fp.sumula_id=fs.id LEFT JOIN football_matches fm ON fm.sumula_id=fs.id"
    if conditions: sql += " WHERE " + " AND ".join(conditions)
    sql += " GROUP BY fs.id ORDER BY fs.match_date DESC,fs.id DESC"
    rows = db.execute(sql, tuple(params)).fetchall()
    return render_template("football_sumulas.html", rows=rows, situations=SITUATIONS, start=start, end=end, situation=situation)


@bp.route("/sumulas/nova", methods=["GET", "POST"])
@roles_allowed("manager", "football_manager")
def new_sumula():
    if request.method == "POST":
        db = get_db()
        try:
            match_date = _match_day(request.form.get("match_date"))
            local = request.form.get("local", "").strip()[:200]
            horario = request.form.get("horario", "").strip()[:30]
            observacoes = request.form.get("observacoes", "").strip()[:5000]
            if db.execute("SELECT 1 FROM football_sumulas WHERE match_date=?", (match_date.isoformat(),)).fetchone():
                raise ValueError("Já existe uma súmula cadastrada para essa data.")
            day = "QUARTA" if match_date.weekday() == 2 else "SABADO"
            with db:
                cur = db.execute("INSERT INTO football_sumulas(match_date,day_pelada,local,horario,situacao,observacoes,created_by) VALUES(?,?,?,?,'RASCUNHO',?,?)", (match_date.isoformat(), day, local, horario, observacoes, g.user["id"]))
                sid = cur.lastrowid
                db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,1)", (sid,))
                _audit(db, sid, "CRIADA", f"{day} {match_date.isoformat()}")
            flash("Súmula criada com a 1ª partida. Adicione a 2ª quando necessário.", "success")
            return redirect(url_for("football.detail", sumula_id=sid))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception:
            db.rollback()
            flash("Não foi possível criar a súmula.", "danger")
    return render_template("football_form.html", sumula=None, today=local_today().isoformat())


@bp.route("/sumulas/<int:sumula_id>", methods=["GET", "POST"])
@roles_allowed("manager", "football_manager")
def detail(sumula_id):
    db = get_db()
    try:
        audit_page = max(1, int(request.args.get("audit_page", "1")))
    except ValueError:
        audit_page = 1
    data = _sumula(db, sumula_id, audit_page=audit_page)
    if not data:
        flash("Súmula não encontrada.", "danger")
        return redirect(url_for("football.sumulas"))
    if request.method == "POST":
        action = request.form.get("action", "")
        math_results = None
        try:
            sumula = data[0]
            if sumula["locked_at"]:
                raise ValueError("A súmula foi encerrada definitivamente e não aceita novas alterações.")
            if sumula["situacao"] in ("FINALIZADA", "CANCELADA") and action not in ("status", "lock"):
                raise ValueError("A súmula está bloqueada para alterações. Reabra-a antes de editar.")
            if action == "participant":
                player_id = int(request.form.get("player_id", ""))
                if not _eligible_player(db, player_id):
                    raise ValueError("Veteranos e mulheres não participam das partidas de futebol.")
                if db.execute("SELECT 1 FROM football_participants WHERE sumula_id=? AND player_id=?", (sumula_id, player_id)).fetchone(): raise ValueError("Este peladeiro já está na súmula.")
                preferred_position = request.form.get("preferred_position", "").strip().upper()
                if not preferred_position:
                    player_position = db.execute("SELECT football_position FROM players WHERE id=?", (player_id,)).fetchone()
                    preferred_position = _lineup_position(player_position["football_position"]) if player_position else ""
                draw_order = request.form.get("draw_order", "").strip()
                if draw_order:
                    draw_order = int(draw_order)
                    if draw_order < 1 or draw_order > 44:
                        raise ValueError("A ordem do sorteio deve estar entre 1 e 44.")
                    if db.execute("SELECT 1 FROM football_participants WHERE sumula_id=? AND draw_order=?", (sumula_id, draw_order)).fetchone():
                        raise ValueError("Esta ordem de sorteio já está ocupada.")
                db.execute("INSERT INTO football_participants(sumula_id,player_id,status,preferred_position,draw_order,observation) VALUES(?,?,?,?,?,?)", (sumula_id, player_id, request.form.get("status", "CONFIRMADO"), preferred_position, draw_order or None, request.form.get("observation", "").strip()))
                _audit(db, sumula_id, "PARTICIPANTE_ADICIONADO", str(player_id))
            elif action == "lineup":
                if not request.form.get("player_id"):
                    flash("Nenhum jogador selecionado. A escalação é opcional.", "info")
                    return redirect(url_for("football.detail", sumula_id=sumula_id))
                match_id, player_id = int(request.form["match_id"]), int(request.form["player_id"])
                raw_period = request.form.get("period", "").strip()
                period = int(raw_period) if raw_period else 1
                if period not in (1, 2):
                    raise ValueError("O tempo da partida é inválido.")
                if not _eligible_player(db, player_id):
                    raise ValueError("Veteranos e mulheres não podem ser escalados nas partidas de futebol.")
                if not _participant_player(db, sumula_id, player_id):
                    raise ValueError("Escale somente peladeiros participantes desta súmula.")
                lineup_position = _lineup_position(request.form.get("position", ""))
                if lineup_position not in ("GOLEIRO", "DEFENSOR", "MEIO_CAMPO", "ATACANTE"):
                    raise ValueError("Posição inválida para a escalação.")
                lineup_values = (
                    request.form["team"], lineup_position,
                    request.form.get("slot", ""), request.form.get("draw_order") or None,
                    request.form.get("observation", "").strip(),
                )
                existing_lineup = db.execute(
                    "SELECT id FROM football_lineups WHERE match_id=? AND player_id=? AND period=?",
                    (match_id, player_id, period),
                ).fetchone()
                if existing_lineup:
                    db.execute(
                        "UPDATE football_lineups SET team=?,position=?,slot=?,draw_order=?,observation=? WHERE id=?",
                        (*lineup_values, existing_lineup["id"]),
                    )
                    _audit(db, sumula_id, "ESCALACAO_ATUALIZADA", f"{player_id} · Tempo {period}")
                else:
                    db.execute(
                        "INSERT INTO football_lineups(match_id,player_id,team,position,slot,draw_order,observation,period) VALUES(?,?,?,?,?,?,?,?)",
                        (match_id, player_id, *lineup_values, period),
                    )
                    _audit(db, sumula_id, "ESCALACAO_ADICIONADA", f"{player_id} · Tempo {period}")
            elif action == "remove_lineup":
                lineup_id = int(request.form["lineup_id"])
                db.execute("DELETE FROM football_lineups WHERE id=? AND match_id IN (SELECT id FROM football_matches WHERE sumula_id=?)", (lineup_id, sumula_id))
                _audit(db, sumula_id, "ESCALACAO_REMOVIDA", str(lineup_id))
            elif action == "update_participant_order":
                participant_id = int(request.form["participant_id"])
                draw_order = int(request.form.get("draw_order", "0"))
                if draw_order < 1 or draw_order > 44:
                    raise ValueError("A ordem do sorteio deve estar entre 1 e 44.")
                if db.execute("SELECT 1 FROM football_participants WHERE sumula_id=? AND draw_order=? AND id!=?", (sumula_id, draw_order, participant_id)).fetchone():
                    raise ValueError("Esta ordem de sorteio já está ocupada.")
                db.execute("UPDATE football_participants SET draw_order=? WHERE id=? AND sumula_id=?", (draw_order, participant_id, sumula_id))
                db.execute("DELETE FROM football_responsibles WHERE sumula_id=? AND SUBSTR(observation,1,17)='REGRA_AUTOMATICA_'", (sumula_id,))
                _audit(db, sumula_id, "ORDEM_PARTICIPANTE_ATUALIZADA", f"{participant_id}:{draw_order}")
            elif action == "score":
                match_id = int(request.form["match_id"]); blue, white = max(0, int(request.form.get("blue_score", 0))), max(0, int(request.form.get("white_score", 0)))
                if not db.execute("SELECT 1 FROM football_matches WHERE id=? AND sumula_id=?", (match_id, sumula_id)).fetchone():
                    raise ValueError("Partida inválida para esta súmula.")
                for team, score in (("AZUL", blue), ("BRANCO", white)):
                    goals_count = int(db.execute("SELECT COUNT(*) FROM football_goals WHERE match_id=? AND benefited_team=?", (match_id, team)).fetchone()[0] or 0)
                    if goals_count > score:
                        raise ValueError(f"O placar do {team.title()} não pode ser menor que os gols já registrados ({goals_count}).")
                db.execute("UPDATE football_matches SET blue_score=?,white_score=?,status='ENCERRADA' WHERE id=? AND sumula_id=?", (blue, white, match_id, sumula_id)); _audit(db, sumula_id, "RESULTADO_ATUALIZADO", f"{blue} x {white}")
            elif action == "goal":
                author_player_id = int(request.form["author_player_id"]) if request.form.get("author_player_id") else None
                if author_player_id and not _participant_player(db, sumula_id, author_player_id):
                    raise ValueError("Registre gols somente para peladeiros participantes desta súmula.")
                assist_player_id = int(request.form["assist_player_id"]) if request.form.get("assist_player_id") else None
                if assist_player_id and not _participant_player(db, sumula_id, assist_player_id):
                    raise ValueError("Registre assistências somente para peladeiros participantes desta súmula.")
                match_id = int(request.form["match_id"])
                benefited_team = request.form["benefited_team"]
                if benefited_team not in TEAMS or not db.execute("SELECT 1 FROM football_matches WHERE id=? AND sumula_id=?", (match_id, sumula_id)).fetchone():
                    raise ValueError("Partida ou time inválido para esta súmula.")
                _ensure_goal_fits_score(db, match_id, benefited_team)
                db.execute("INSERT INTO football_goals(match_id,author_player_id,benefited_team,assist_player_id,minute,own_goal,observation,created_by) VALUES(?,?,?,?,?,?,?,?)", (match_id, author_player_id, benefited_team, assist_player_id, int(request.form["minute"]) if request.form.get("minute") else None, 1 if request.form.get("own_goal") else 0, request.form.get("observation", "").strip(), g.user["id"])); _audit(db, sumula_id, "GOL_REGISTRADO")
            elif action in ("update_goal", "move_goal", "delete_goal"):
                goal_id = int(request.form["goal_id"])
                goal = db.execute("SELECT fg.id,fg.match_id,fm.sumula_id FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id WHERE fg.id=? AND fm.sumula_id=?", (goal_id, sumula_id)).fetchone()
                if not goal:
                    raise ValueError("Gol não encontrado nesta súmula.")
                if action == "delete_goal":
                    db.execute("DELETE FROM football_goals WHERE id=?", (goal_id,))
                    _audit(db, sumula_id, "GOL_EXCLUIDO", str(goal_id))
                else:
                    target_match_id = int(request.form["match_id"])
                    if not db.execute("SELECT 1 FROM football_matches WHERE id=? AND sumula_id=?", (target_match_id, sumula_id)).fetchone():
                        raise ValueError("Partida inválida para esta súmula.")
                    if action == "move_goal":
                        _ensure_goal_fits_score(db, target_match_id, db.execute("SELECT benefited_team FROM football_goals WHERE id=?", (goal_id,)).fetchone()[0], goal_id)
                        db.execute("UPDATE football_goals SET match_id=? WHERE id=?", (target_match_id, goal_id))
                        _audit(db, sumula_id, "GOL_MOVIDO", f"{goal['match_id']}->{target_match_id}")
                    else:
                        author_player_id = int(request.form["author_player_id"]) if request.form.get("author_player_id") else None
                        assist_player_id = int(request.form["assist_player_id"]) if request.form.get("assist_player_id") else None
                        for player_id, label in ((author_player_id, "autor"), (assist_player_id, "assistência")):
                            if player_id and not _participant_player(db, sumula_id, player_id):
                                raise ValueError(f"Selecione um participante válido para {label}.")
                        benefited_team = request.form.get("benefited_team", "")
                        if benefited_team not in TEAMS:
                            raise ValueError("Time inválido.")
                        minute = int(request.form["minute"]) if request.form.get("minute") else None
                        _ensure_goal_fits_score(db, target_match_id, benefited_team, goal_id)
                        db.execute("UPDATE football_goals SET match_id=?,author_player_id=?,benefited_team=?,assist_player_id=?,minute=? WHERE id=?", (target_match_id, author_player_id, benefited_team, assist_player_id, minute, goal_id))
                        _audit(db, sumula_id, "GOL_EDITADO", str(goal_id))
            elif action == "incident":
                description = request.form.get("description", "").strip()
                card = request.form.get("card", "").strip().upper()
                if card and card not in CARD_TYPES: raise ValueError("Cartão inválido.")
                if card:
                    description = description or f"Cartão {CARD_TYPES[card]}"
                    level = "INFORMATIVO"
                else:
                    if not description: raise ValueError("Descreva a ocorrência.")
                    level = request.form["level"]
                db.execute("INSERT INTO football_incidents(sumula_id,match_id,type,level,player_id,card,description,created_by) VALUES(?,?,?,?,?,?,?,?)", (sumula_id, int(request.form["match_id"]) if request.form.get("match_id") else None, request.form["type"], level, int(request.form["player_id"]) if request.form.get("player_id") else None, card, description, g.user["id"])); _audit(db, sumula_id, "OCORRENCIA_REGISTRADA")
            elif action in ("responsible", "update_responsible"):
                responsibility_type = request.form.get("responsibility_type", "")
                if responsibility_type not in ("SORTEIO", "SUMULA", "QUADRO", "GOLEIRO_VOLUNTARIO", "ARBITRO_VOLUNTARIO", "OUTRO"):
                    raise ValueError("Tipo de responsável inválido.")
                match_id = int(request.form["match_id"]) if request.form.get("match_id") else None
                if responsibility_type == "ARBITRO_VOLUNTARIO" and not match_id:
                    raise ValueError("Selecione a partida do árbitro.")
                if match_id and not db.execute("SELECT 1 FROM football_matches WHERE id=? AND sumula_id=?", (match_id, sumula_id)).fetchone():
                    raise ValueError("Partida inválida para esta súmula.")
                player_id = int(request.form["player_id"]) if request.form.get("player_id") else None
                observation = request.form.get("observation", "").strip()
                responsible_id = int(request.form["responsible_id"]) if request.form.get("responsible_id") else None
                if action == "update_responsible":
                    if not responsible_id or not db.execute("SELECT 1 FROM football_responsibles WHERE id=? AND sumula_id=?", (responsible_id, sumula_id)).fetchone():
                        raise ValueError("Responsável não encontrado nesta súmula.")
                    db.execute("UPDATE football_responsibles SET match_id=?,player_id=?,responsibility_type=?,observation=? WHERE id=? AND sumula_id=?", (match_id, player_id, responsibility_type, observation, responsible_id, sumula_id))
                    _audit(db, sumula_id, "RESPONSAVEL_ATUALIZADO", f"{responsible_id}: {responsibility_type}")
                else:
                    db.execute("INSERT INTO football_responsibles(sumula_id,match_id,player_id,responsibility_type,observation) VALUES(?,?,?,?,?)", (sumula_id, match_id, player_id, responsibility_type, observation))
                    _audit(db, sumula_id, "RESPONSAVEL_REGISTRADO", responsibility_type)
                if responsibility_type == "GOLEIRO_VOLUNTARIO" and match_id:
                    db.execute("DELETE FROM football_responsibles WHERE sumula_id=? AND match_id=? AND SUBSTR(observation,1,17)='REGRA_AUTOMATICA_'", (sumula_id, match_id))
            elif action == "delete_responsible":
                responsible_id = int(request.form.get("responsible_id", "0"))
                if not db.execute("SELECT 1 FROM football_responsibles WHERE id=? AND sumula_id=?", (responsible_id, sumula_id)).fetchone():
                    raise ValueError("Responsável não encontrado nesta súmula.")
                db.execute("DELETE FROM football_responsibles WHERE id=? AND sumula_id=?", (responsible_id, sumula_id))
                _audit(db, sumula_id, "RESPONSAVEL_EXCLUIDO", str(responsible_id))
            elif action == "apply_fallback_roles":
                roles = _fallback_roles(db, sumula_id)
                if not roles:
                    flash("A regra não pode ser aplicada: já há um goleiro voluntário, ela já foi aplicada ou faltam participantes confirmados.", "info")
                    return redirect(url_for("football.detail", sumula_id=sumula_id))
                for role in roles:
                    observation = f"REGRA_AUTOMATICA_{role['role'].upper()}_ORDEM_{role['draw_order']}"
                    db.execute("INSERT INTO football_responsibles(sumula_id,match_id,player_id,responsibility_type,observation) VALUES(?,?,?,?,?)", (sumula_id, role["match_id"], role["player_id"], "OUTRO", observation))
                _audit(db, sumula_id, "REGRA_AUTOMATICA_APLICADA", ", ".join(f"{role['role']}: {role['name']}" for role in roles))
            elif action == "third_match":
                if not db.execute("SELECT 1 FROM football_matches WHERE sumula_id=? AND number=2", (sumula_id,)).fetchone():
                    raise ValueError("Adicione a 2ª partida antes da 3ª.")
                if db.execute("SELECT 1 FROM football_matches WHERE sumula_id=? AND number=3", (sumula_id,)).fetchone():
                    raise ValueError("A terceira partida já existe.")
                db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,3)", (sumula_id,)); _audit(db, sumula_id, "TERCEIRA_PARTIDA_ADICIONADA")
            elif action == "second_match":
                if db.execute("SELECT 1 FROM football_matches WHERE sumula_id=? AND number=2", (sumula_id,)).fetchone():
                    raise ValueError("A segunda partida já existe.")
                db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,2)", (sumula_id,)); _audit(db, sumula_id, "SEGUNDA_PARTIDA_ADICIONADA")
            elif action == "delete_draft":
                if sumula["situacao"] != "RASCUNHO":
                    raise ValueError("Somente súmulas em rascunho podem ser excluídas.")
                db.execute("INSERT INTO football_deleted_sumula_audit(sumula_id,match_date,day_pelada,local,deleted_by) VALUES(?,?,?,?,?)", (sumula_id, sumula["match_date"], sumula["day_pelada"], sumula["local"] or "", g.user["id"]))
                db.execute("DELETE FROM football_sumulas WHERE id=?", (sumula_id,))
                db.commit()
                flash("Súmula em rascunho excluída.", "success")
                return redirect(url_for("football.sumulas"))
            elif action == "lock":
                if sumula["situacao"] != "FINALIZADA":
                    raise ValueError("Finalize a súmula antes de encerrá-la definitivamente.")
                mismatches = _score_mismatches(db, data[2])
                if mismatches:
                    raise ValueError("O placar não corresponde aos gols registrados (" + ", ".join(mismatches) + "). Corrija antes do encerramento definitivo.")
                db.execute("UPDATE football_sumulas SET locked_at=CURRENT_TIMESTAMP,locked_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (g.user["id"], sumula_id))
                _audit(db, sumula_id, "ENCERRAMENTO_DEFINITIVO", "Conferência concluída")
            elif action == "status":
                new_status = request.form["situacao"]
                if new_status not in SITUATIONS: raise ValueError("Situação inválida.")
                if new_status == "RASCUNHO" and sumula["situacao"] != "CANCELADA":
                    raise ValueError("Use a situação Aberta ou Em andamento para continuar a súmula.")
                if sumula["situacao"] == "FINALIZADA" and new_status not in ("EM_ANDAMENTO", "CANCELADA"):
                    raise ValueError("Uma súmula finalizada só pode ser reaberta para edição ou cancelada.")
                if sumula["situacao"] == "FINALIZADA" and new_status == "EM_ANDAMENTO" and not request.form.get("justification", "").strip():
                    raise ValueError("Informe a justificativa para reabrir a súmula.")
                if new_status == "FINALIZADA":
                    mismatches = _score_mismatches(db, data[2])
                    if mismatches and not request.form.get("justification", "").strip():
                        raise ValueError("O placar não corresponde aos gols registrados (" + ", ".join(mismatches) + "). Informe uma justificativa.")
                    db.execute("UPDATE football_sumulas SET situacao=?,finalized_at=CURRENT_TIMESTAMP,reopen_justification=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, request.form.get("justification", "").strip(), sumula_id))
                    math_results = _matematico_results(db, sumula_id)
                elif new_status == "CANCELADA": db.execute("UPDATE football_sumulas SET situacao=?,canceled_at=CURRENT_TIMESTAMP,canceled_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, g.user["id"], sumula_id))
                else: db.execute("UPDATE football_sumulas SET situacao=?,reopen_justification=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, request.form.get("justification", "").strip(), sumula_id))
                _audit(db, sumula_id, "SITUACAO_ATUALIZADA", f"{new_status}: {request.form.get('justification', '').strip()}")
            else: raise ValueError("Ação inválida.")
            db.commit()
            if math_results:
                result_text = "; ".join(f"Partida {row['number']}: Azul {row['blue_score']} x {row['white_score']} Branco" for row in math_results)
                title = "É Matemático! ⚽"
                body = f"A divisão dos times foi equilibrada na súmula de {sumula['match_date']}: {result_text}."
                recipients = db.execute("SELECT id FROM players WHERE active=1").fetchall()
                for recipient in recipients:
                    send_player_push_once(db, recipient["id"], "matematico_sumula", str(sumula_id), title, body, "/notificacoes", "/static/images/e-matematico.webp")
            flash("Súmula atualizada.", "success")
        except (ValueError, KeyError) as exc:
            db.rollback(); flash(str(exc), "danger")
        return redirect(url_for("football.detail", sumula_id=sumula_id))
    players = db.execute("SELECT id,name,war_name,football_position FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' AND COALESCE(football_position,'')!='APOSENTADO' ORDER BY LOWER(COALESCE(war_name,name)),LOWER(name)").fetchall()
    player_positions = {str(player["id"]): _lineup_position(player["football_position"]) for player in players}
    used_orders = {int(row["draw_order"]) for row in db.execute("SELECT draw_order FROM football_participants WHERE sumula_id=? AND draw_order IS NOT NULL", (sumula_id,)).fetchall()}
    next_draw_order = next((number for number in range(1, 45) if number not in used_orders), 44)
    audit_total = int(db.execute("SELECT COUNT(*) FROM football_audit WHERE sumula_id=?", (sumula_id,)).fetchone()[0] or 0)
    audit_pages = max(1, (audit_total + 4) // 5)
    score_mismatches = _score_mismatches(db, data[2])
    auto_roles = []
    for responsible in data[4]:
        observation = responsible["observation"] or ""
        if observation.startswith("REGRA_AUTOMATICA_"):
            auto_roles.append({"role": "Goleiro" if "_GOLEIRO_" in observation else "Juiz", "name": responsible["war_name"] or responsible["name"] or "Não informado"})
    return render_template("football_detail.html", data=data, players=players, player_positions=player_positions, fallback_roles=_fallback_roles(db, sumula_id), auto_roles=auto_roles, next_draw_order=next_draw_order, situations=SITUATIONS, participant_statuses=PARTICIPANT_STATUSES, positions=POSITIONS, teams=TEAMS, incident_types=INCIDENT_TYPES, incident_levels=INCIDENT_LEVELS, card_types=CARD_TYPES, audit_page=min(audit_page, audit_pages), audit_pages=1, score_mismatches=score_mismatches)


@bp.get("/sumulas/<int:sumula_id>/imprimir")
@roles_allowed("manager", "football_manager")
def print_sumula(sumula_id):
    data = _sumula(get_db(), sumula_id)
    if not data:
        flash("Súmula não encontrada.", "danger")
        return redirect(url_for("football.sumulas"))
    return render_template("football_print.html", data=data, positions=POSITIONS, teams=TEAMS, incident_types=INCIDENT_TYPES, card_types=CARD_TYPES)
