from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import local_today

bp = Blueprint("football", __name__, url_prefix="/futebol")

SITUATIONS = {"RASCUNHO": "Rascunho", "ABERTA": "Aberta", "EM_ANDAMENTO": "Em andamento", "FINALIZADA": "Finalizada", "CANCELADA": "Cancelada"}
PARTICIPANT_STATUSES = {"CONFIRMADO": "Confirmado", "AUSENTE": "Ausente", "DESISTENTE": "Desistente", "RESERVA": "Reserva"}
POSITIONS = {"GOLEIRO": "Goleiro", "DEFENSOR": "Defensor", "MEIO_CAMPO": "Meio-campo", "ATACANTE": "Atacante"}
TEAMS = {"AZUL": "Azul", "BRANCO": "Branco"}
INCIDENT_TYPES = {"DISCIPLINAR": "Disciplinar", "LESAO": "Lesão", "ATRASO": "Atraso", "ABANDONO_PARTIDA": "Abandono de partida", "DISCUSSAO": "Discussão", "FALHA_ORGANIZACAO": "Falha de organização", "PROBLEMA_ESTRUTURAL": "Problema estrutural", "OUTRO": "Outro"}
INCIDENT_LEVELS = {"INFORMATIVO": "Informativo", "ATENCAO": "Atenção", "GRAVE": "Grave"}


def _audit(db, sumula_id, action, details=""):
    db.execute("INSERT INTO football_audit(sumula_id,user_id,action,details) VALUES(?,?,?,?)", (sumula_id, g.user["id"], action, details))


def _eligible_player(db, player_id):
    return db.execute("SELECT id FROM players WHERE id=? AND active=1 AND gender!='female' AND membership_type!='veteran'", (player_id,)).fetchone()


def _match_day(value):
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError("Informe uma data válida para a pelada.")
    if parsed.weekday() not in (2, 5):
        raise ValueError("A data deve cair em uma quarta-feira ou sábado.")
    return parsed


def _sumula(db, sumula_id):
    row = db.execute("SELECT fs.*,u.name created_by_name FROM football_sumulas fs LEFT JOIN users u ON u.id=fs.created_by WHERE fs.id=?", (sumula_id,)).fetchone()
    if not row:
        return None
    participants = db.execute("SELECT fp.*,p.name,p.war_name,p.photo_data,p.thumbnail_data,p.football_position FROM football_participants fp JOIN players p ON p.id=fp.player_id WHERE fp.sumula_id=? ORDER BY COALESCE(fp.draw_order,999999),LOWER(p.war_name),LOWER(p.name)", (sumula_id,)).fetchall()
    matches = []
    for match in db.execute("SELECT * FROM football_matches WHERE sumula_id=? ORDER BY number", (sumula_id,)).fetchall():
        lineups = db.execute("SELECT fl.*,p.name,p.war_name FROM football_lineups fl JOIN players p ON p.id=fl.player_id WHERE fl.match_id=? ORDER BY fl.team,fl.position,COALESCE(fl.draw_order,999999),LOWER(p.name)", (match["id"],)).fetchall()
        goals = db.execute("SELECT fg.*,pa.name author_name,pa.war_name author_war,ps.name assist_name,ps.war_name assist_war FROM football_goals fg LEFT JOIN players pa ON pa.id=fg.author_player_id LEFT JOIN players ps ON ps.id=fg.assist_player_id WHERE fg.match_id=? ORDER BY fg.id", (match["id"],)).fetchall()
        matches.append({"row": match, "lineups": lineups, "goals": goals})
    incidents = db.execute("SELECT fi.*,p.name,p.war_name FROM football_incidents fi LEFT JOIN players p ON p.id=fi.player_id WHERE fi.sumula_id=? ORDER BY fi.id DESC", (sumula_id,)).fetchall()
    responsibles = db.execute("SELECT fr.*,p.name,p.war_name FROM football_responsibles fr LEFT JOIN players p ON p.id=fr.player_id WHERE fr.sumula_id=? ORDER BY fr.id", (sumula_id,)).fetchall()
    audits = db.execute("SELECT fa.*,u.name user_name FROM football_audit fa LEFT JOIN users u ON u.id=fa.user_id WHERE fa.sumula_id=? ORDER BY fa.id DESC LIMIT 30", (sumula_id,)).fetchall()
    return row, participants, matches, incidents, responsibles, audits


@bp.get("")
@roles_allowed("manager", "football_manager")
def dashboard():
    db = get_db()
    metrics = db.execute("SELECT COUNT(*) total,COUNT(CASE WHEN situacao='FINALIZADA' THEN 1 END) finalized,COUNT(CASE WHEN situacao IN ('ABERTA','EM_ANDAMENTO') THEN 1 END) active,COUNT(CASE WHEN match_date>=? AND situacao!='CANCELADA' THEN 1 END) upcoming FROM football_sumulas", (local_today().isoformat(),)).fetchone()
    recent = db.execute("SELECT * FROM football_sumulas WHERE situacao!='CANCELADA' ORDER BY match_date DESC,id DESC LIMIT 8").fetchall()
    return render_template("football_dashboard.html", metrics=metrics, recent=recent, situations=SITUATIONS)


@bp.get("/estatisticas")
@roles_allowed("manager", "football_manager")
def statistics():
    db = get_db()
    totals = db.execute("SELECT COUNT(DISTINCT fs.id) sumulas,COUNT(DISTINCT fm.id) partidas,COUNT(DISTINCT fg.id) gols FROM football_sumulas fs LEFT JOIN football_matches fm ON fm.sumula_id=fs.id AND fm.status='ENCERRADA' LEFT JOIN football_goals fg ON fg.match_id=fm.id WHERE fs.situacao='FINALIZADA'").fetchone()
    finalized_sumulas = int(totals["sumulas"] or 0)
    player_stats = []
    for player in db.execute("SELECT id,name,war_name FROM players WHERE active=1 ORDER BY LOWER(name)").fetchall():
        participacoes = int(db.execute("SELECT COUNT(DISTINCT sumula_id) FROM football_participants WHERE player_id=? AND status='CONFIRMADO' AND sumula_id IN (SELECT id FROM football_sumulas WHERE situacao='FINALIZADA')", (player["id"],)).fetchone()[0] or 0)
        games = db.execute("""SELECT fl.team,fm.blue_score,fm.white_score FROM football_lineups fl
            JOIN football_matches fm ON fm.id=fl.match_id AND fm.status='ENCERRADA'
            JOIN football_sumulas fs ON fs.id=fm.sumula_id AND fs.situacao='FINALIZADA'
            WHERE fl.player_id=?""", (player["id"],)).fetchall()
        wins = draws = losses = 0
        for game in games:
            own, opponent = (int(game["blue_score"] or 0), int(game["white_score"] or 0)) if game["team"] == "AZUL" else (int(game["white_score"] or 0), int(game["blue_score"] or 0))
            if own > opponent: wins += 1
            elif own == opponent: draws += 1
            else: losses += 1
        goals = int(db.execute("SELECT COUNT(*) FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fg.author_player_id=? AND fm.status='ENCERRADA' AND fs.situacao='FINALIZADA'", (player["id"],)).fetchone()[0] or 0)
        assists = int(db.execute("SELECT COUNT(*) FROM football_goals fg JOIN football_matches fm ON fm.id=fg.match_id JOIN football_sumulas fs ON fs.id=fm.sumula_id WHERE fg.assist_player_id=? AND fm.status='ENCERRADA' AND fs.situacao='FINALIZADA'", (player["id"],)).fetchone()[0] or 0)
        historical = db.execute("SELECT COALESCE(SUM(goals),0) goals,COALESCE(SUM(assists),0) assists FROM football_historical_stats WHERE player_id=?", (player["id"],)).fetchone()
        goals += int(historical["goals"] or 0); assists += int(historical["assists"] or 0)
        if participacoes or games or goals or assists:
            player_stats.append({"id": player["id"], "name": player["name"], "war_name": player["war_name"], "participacoes": participacoes, "frequencia": round((participacoes / finalized_sumulas) * 100, 1) if finalized_sumulas else 0, "jogos": len(games), "vitorias": wins, "empates": draws, "derrotas": losses, "gols": goals, "assistencias": assists})
    player_stats.sort(key=lambda item: (-item["gols"], -item["assistencias"], -item["vitorias"], -item["participacoes"], (item["war_name"] or item["name"]).lower()))
    team_results = db.execute("""SELECT fm.*,fs.match_date FROM football_matches fm JOIN football_sumulas fs ON fs.id=fm.sumula_id
        WHERE fm.status='ENCERRADA' ORDER BY fs.match_date DESC,fm.number DESC LIMIT 20""").fetchall()
    return render_template("football_statistics.html", totals=totals, player_stats=player_stats, team_results=team_results)


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
            if not db.execute("SELECT 1 FROM players WHERE id=? AND active=1", (player_id,)).fetchone(): raise ValueError("Selecione um peladeiro válido.")
            if goals == 0 and assists == 0: raise ValueError("Informe pelo menos um gol ou uma assistência.")
            db.execute("INSERT INTO football_historical_stats(player_id,stat_date,goals,assists,notes,created_by) VALUES(?,?,?,?,?,?)", (player_id, stat_date, goals, assists, notes, g.user["id"]))
            db.commit(); flash("Lançamento histórico registrado.", "success")
        except (ValueError, KeyError):
            db.rollback(); flash("Informe peladeiro, data e pelo menos um gol ou assistência válidos.", "danger")
        return redirect(url_for("football.historical_stats"))
    players = db.execute("SELECT id,name,war_name FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' ORDER BY LOWER(COALESCE(war_name,name))").fetchall()
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
    own = {"participacoes": 0, "jogos": 0, "vitorias": 0, "empates": 0, "derrotas": 0, "gols": 0, "assistencias": 0}
    if player_id:
        own["participacoes"] = int(db.execute("SELECT COUNT(DISTINCT sumula_id) FROM football_participants WHERE player_id=? AND status='CONFIRMADO' AND sumula_id IN (SELECT id FROM football_sumulas WHERE situacao='FINALIZADA')", (player_id,)).fetchone()[0] or 0)
        games = db.execute("""SELECT fl.team,fm.blue_score,fm.white_score FROM football_lineups fl JOIN football_matches fm ON fm.id=fl.match_id AND fm.status='ENCERRADA' JOIN football_sumulas fs ON fs.id=fm.sumula_id AND fs.situacao='FINALIZADA' WHERE fl.player_id=?""", (player_id,)).fetchall()
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
    if situation in SITUATIONS:
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
                db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,2)", (sid,))
                _audit(db, sid, "CRIADA", f"{day} {match_date.isoformat()}")
            flash("Súmula criada com duas partidas.", "success")
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
    data = _sumula(db, sumula_id)
    if not data:
        flash("Súmula não encontrada.", "danger")
        return redirect(url_for("football.sumulas"))
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            sumula = data[0]
            if sumula["situacao"] in ("FINALIZADA", "CANCELADA") and action not in ("status",):
                raise ValueError("A súmula está bloqueada para alterações. Reabra-a antes de editar.")
            if action == "participant":
                player_id = int(request.form.get("player_id", ""))
                if not _eligible_player(db, player_id):
                    raise ValueError("Veteranos e mulheres não participam das partidas de futebol.")
                if db.execute("SELECT 1 FROM football_participants WHERE sumula_id=? AND player_id=?", (sumula_id, player_id)).fetchone(): raise ValueError("Este peladeiro já está na súmula.")
                preferred_position = request.form.get("preferred_position", "").strip().upper()
                if not preferred_position:
                    player_position = db.execute("SELECT football_position FROM players WHERE id=?", (player_id,)).fetchone()
                    preferred_position = (player_position["football_position"] or "") if player_position else ""
                db.execute("INSERT INTO football_participants(sumula_id,player_id,status,preferred_position,draw_order,observation) VALUES(?,?,?,?,?,?)", (sumula_id, player_id, request.form.get("status", "CONFIRMADO"), preferred_position, request.form.get("draw_order") or None, request.form.get("observation", "").strip()))
                _audit(db, sumula_id, "PARTICIPANTE_ADICIONADO", str(player_id))
            elif action == "lineup":
                match_id, player_id = int(request.form["match_id"]), int(request.form["player_id"])
                if not _eligible_player(db, player_id):
                    raise ValueError("Veteranos e mulheres não podem ser escalados nas partidas de futebol.")
                if db.execute("SELECT 1 FROM football_lineups WHERE match_id=? AND player_id=?", (match_id, player_id)).fetchone(): raise ValueError("O peladeiro já está escalado nesta partida.")
                db.execute("INSERT INTO football_lineups(match_id,player_id,team,position,slot,draw_order,observation) VALUES(?,?,?,?,?,?,?)", (match_id, player_id, request.form["team"], request.form["position"], request.form.get("slot", ""), request.form.get("draw_order") or None, request.form.get("observation", "").strip()))
                _audit(db, sumula_id, "ESCALACAO_ADICIONADA", str(player_id))
            elif action == "score":
                match_id = int(request.form["match_id"]); blue, white = max(0, int(request.form.get("blue_score", 0))), max(0, int(request.form.get("white_score", 0)))
                db.execute("UPDATE football_matches SET blue_score=?,white_score=?,status='ENCERRADA' WHERE id=? AND sumula_id=?", (blue, white, match_id, sumula_id)); _audit(db, sumula_id, "RESULTADO_ATUALIZADO", f"{blue} x {white}")
            elif action == "goal":
                db.execute("INSERT INTO football_goals(match_id,author_player_id,benefited_team,assist_player_id,minute,own_goal,observation,created_by) VALUES(?,?,?,?,?,?,?,?)", (int(request.form["match_id"]), int(request.form["author_player_id"]) if request.form.get("author_player_id") else None, request.form["benefited_team"], int(request.form["assist_player_id"]) if request.form.get("assist_player_id") else None, int(request.form["minute"]) if request.form.get("minute") else None, 1 if request.form.get("own_goal") else 0, request.form.get("observation", "").strip(), g.user["id"])); _audit(db, sumula_id, "GOL_REGISTRADO")
            elif action == "incident":
                description = request.form.get("description", "").strip()
                if not description: raise ValueError("Descreva a ocorrência.")
                db.execute("INSERT INTO football_incidents(sumula_id,match_id,type,level,player_id,description,created_by) VALUES(?,?,?,?,?,?,?)", (sumula_id, int(request.form["match_id"]) if request.form.get("match_id") else None, request.form["type"], request.form["level"], int(request.form["player_id"]) if request.form.get("player_id") else None, description, g.user["id"])); _audit(db, sumula_id, "OCORRENCIA_REGISTRADA")
            elif action == "responsible":
                responsibility_type = request.form.get("responsibility_type", "")
                if responsibility_type not in ("SORTEIO", "SUMULA", "QUADRO", "GOLEIRO_VOLUNTARIO", "ARBITRO_VOLUNTARIO", "OUTRO"):
                    raise ValueError("Tipo de responsável inválido.")
                db.execute("INSERT INTO football_responsibles(sumula_id,player_id,responsibility_type,observation) VALUES(?,?,?,?)", (sumula_id, int(request.form["player_id"]) if request.form.get("player_id") else None, responsibility_type, request.form.get("observation", "").strip()))
                _audit(db, sumula_id, "RESPONSAVEL_REGISTRADO", responsibility_type)
            elif action == "third_match":
                if db.execute("SELECT 1 FROM football_matches WHERE sumula_id=? AND number=3", (sumula_id,)).fetchone():
                    raise ValueError("A terceira partida já existe.")
                db.execute("INSERT INTO football_matches(sumula_id,number) VALUES(?,3)", (sumula_id,)); _audit(db, sumula_id, "TERCEIRA_PARTIDA_ADICIONADA")
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
                    mismatches = []
                    for item in data[2]:
                        match = item["row"]
                        goals = db.execute("SELECT benefited_team,COUNT(*) total FROM football_goals WHERE match_id=? GROUP BY benefited_team", (match["id"],)).fetchall()
                        counts = {row["benefited_team"]: int(row["total"]) for row in goals}
                        if counts.get("AZUL", 0) != int(match["blue_score"] or 0) or counts.get("BRANCO", 0) != int(match["white_score"] or 0):
                            mismatches.append(f"{match['number']}ª partida")
                    if mismatches and not request.form.get("justification", "").strip():
                        raise ValueError("O placar não corresponde aos gols registrados (" + ", ".join(mismatches) + "). Informe uma justificativa.")
                    db.execute("UPDATE football_sumulas SET situacao=?,finalized_at=CURRENT_TIMESTAMP,reopen_justification=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, request.form.get("justification", "").strip(), sumula_id))
                elif new_status == "CANCELADA": db.execute("UPDATE football_sumulas SET situacao=?,canceled_at=CURRENT_TIMESTAMP,canceled_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, g.user["id"], sumula_id))
                else: db.execute("UPDATE football_sumulas SET situacao=?,reopen_justification=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, request.form.get("justification", "").strip(), sumula_id))
                _audit(db, sumula_id, "SITUACAO_ATUALIZADA", f"{new_status}: {request.form.get('justification', '').strip()}")
            else: raise ValueError("Ação inválida.")
            db.commit(); flash("Súmula atualizada.", "success")
        except (ValueError, KeyError) as exc:
            db.rollback(); flash(str(exc), "danger")
        return redirect(url_for("football.detail", sumula_id=sumula_id))
    players = db.execute("SELECT id,name,war_name,football_position FROM players WHERE active=1 AND gender!='female' AND membership_type!='veteran' ORDER BY LOWER(COALESCE(war_name,name)),LOWER(name)").fetchall()
    player_positions = {str(player["id"]): (player["football_position"] or "") for player in players}
    return render_template("football_detail.html", data=data, players=players, player_positions=player_positions, situations=SITUATIONS, participant_statuses=PARTICIPANT_STATUSES, positions=POSITIONS, teams=TEAMS, incident_types=INCIDENT_TYPES, incident_levels=INCIDENT_LEVELS)


@bp.get("/sumulas/<int:sumula_id>/imprimir")
@roles_allowed("manager", "football_manager")
def print_sumula(sumula_id):
    data = _sumula(get_db(), sumula_id)
    if not data:
        flash("Súmula não encontrada.", "danger")
        return redirect(url_for("football.sumulas"))
    return render_template("football_print.html", data=data, positions=POSITIONS, teams=TEAMS, incident_types=INCIDENT_TYPES)
