from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from src.db import get_db
from src.routes.auth import roles_allowed
from src.utils import normalize_cpf, spreadsheet_rows

bp = Blueprint("players", __name__)

@bp.route("/players", methods=["GET", "POST"])
@roles_allowed("manager")
def players():
    db = get_db()
    if request.method == "POST":
        try:
            membership_type = request.form.get("membership_type", "regular")
            if membership_type not in ("regular", "goalkeeper", "board"):
                raise ValueError("Classificação financeira inválida.")
            
            db.execute(
                """INSERT INTO players
                (name,war_name,cpf,phone,emergency_phone,email,membership_type) VALUES(?,?,?,?,?,?,?)""",
                (
                    request.form["name"].strip(),
                    request.form.get("war_name", "").strip(),
                    normalize_cpf(request.form.get("cpf")),
                    request.form.get("phone", "").strip(),
                    request.form.get("emergency_phone", "").strip(),
                    request.form.get("email", "").strip().lower(),
                    membership_type
                )
            )
            db.commit()
            flash("Peladeiro cadastrado.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao cadastrar peladeiro: {exc}")
            if "unique" in str(exc).lower():
                flash("Já existe um peladeiro com esse nome ou CPF.", "danger")
            else:
                flash("Não foi possível cadastrar o peladeiro devido a um erro interno.", "danger")
        return redirect(url_for("players.players"))

    player_filter = request.args.get("filter", "active")
    filters = {
        "active": ("active=1", ()),
        "regular": ("active=1 AND membership_type=?", ("regular",)),
        "board": ("active=1 AND membership_type=?", ("board",)),
        "goalkeeper": ("active=1 AND membership_type=?", ("goalkeeper",)),
        "inactive": ("active=0", ()),
        "all": ("1=1", ()),
    }
    if player_filter not in filters:
        player_filter = "active"
    where, params = filters[player_filter]
    items = db.execute(f"SELECT * FROM players WHERE {where} ORDER BY active DESC, name", params).fetchall()
    return render_template("players.html", players=items, player_filter=player_filter)

@bp.post("/players/<int:player_id>/membership-type")
@roles_allowed("manager")
def player_membership_type(player_id):
    membership_type = request.form.get("membership_type")
    if membership_type not in ("regular", "goalkeeper", "board"):
        flash("Classificação inválida.", "danger")
    else:
        try:
            db = get_db()
            db.execute("UPDATE players SET membership_type=? WHERE id=?", (membership_type, player_id))
            db.commit()
            flash("Classificação financeira atualizada.", "success")
        except Exception as exc:
            current_app.logger.error(f"Erro ao atualizar classificação do jogador {player_id}: {exc}")
            flash("Erro interno ao atualizar classificação financeira.", "danger")
    return redirect(url_for("players.players"))

@bp.route("/players/<int:player_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager")
def edit_player(player_id):
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    if not player:
        flash("Peladeiro não encontrado.", "warning")
        return redirect(url_for("players.players"))
    
    if request.method == "POST":
        membership_type = request.form.get("membership_type", "regular")
        if membership_type not in ("regular", "goalkeeper", "board"):
            flash("Classificação financeira inválida.", "danger")
        else:
            try:
                db.execute(
                    """UPDATE players SET name=?,war_name=?,cpf=?,email=?,phone=?,emergency_phone=?,membership_type=?
                    WHERE id=?""",
                    (
                        request.form["name"].strip(),
                        request.form.get("war_name", "").strip(),
                        normalize_cpf(request.form.get("cpf")),
                        request.form.get("email", "").strip().lower(),
                        request.form.get("phone", "").strip(),
                        request.form.get("emergency_phone", "").strip(),
                        membership_type,
                        player_id
                    )
                )
                db.commit()
                flash("Cadastro do peladeiro atualizado.", "success")
                return redirect(url_for("players.players"))
            except ValueError as exc:
                flash(str(exc), "danger")
            except Exception as exc:
                current_app.logger.error(f"Erro ao editar jogador {player_id}: {exc}")
                if "unique" in str(exc).lower():
                    flash("Já existe outro peladeiro com esse nome ou CPF.", "danger")
                else:
                    flash("Erro interno ao atualizar cadastro do peladeiro.", "danger")
        player = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    return render_template("edit_player.html", player=player)

@bp.post("/players/<int:player_id>/toggle-active")
@roles_allowed("manager")
def toggle_player_active(player_id):
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    if not player:
        flash("Peladeiro não encontrado.", "warning")
    else:
        try:
            new_status = 0 if player["active"] else 1
            db.execute("UPDATE players SET active=? WHERE id=?", (new_status, player_id))
            db.commit()
            flash("Peladeiro excluído dos cadastros ativos; o histórico foi preservado." if not new_status
                  else "Peladeiro restaurado.", "success")
        except Exception as exc:
            current_app.logger.error(f"Erro ao alternar atividade do jogador {player_id}: {exc}")
            flash("Erro interno ao alterar status do peladeiro.", "danger")
    return redirect(url_for("players.players"))

@bp.post("/players/import")
@roles_allowed("manager")
def import_players():
    upload = request.files.get("spreadsheet")
    if not upload or not upload.filename:
        flash("Escolha uma planilha para importar.", "danger")
        return redirect(url_for("players.players"))
    
    try:
        rows = spreadsheet_rows(upload)
        if not rows:
            raise ValueError("A planilha está vazia.")
        
        from src.utils import normalized_header
        headers = {normalized_header(value): index for index, value in enumerate(rows[0])}
        name_index = next((headers[key] for key in ("nome", "name", "peladeiro") if key in headers), None)
        email_index = next((headers[key] for key in ("email", "emailaddress") if key in headers), None)
        if name_index is None or email_index is None:
            raise ValueError("A primeira linha precisa ter as colunas Nome e E-mail.")
        
        imported = updated = skipped = 0
        db = get_db()
        with db:
            for row in rows[1:]:
                name = str(row[name_index] or "").strip() if name_index < len(row) else ""
                email = str(row[email_index] or "").strip().lower() if email_index < len(row) else ""
                if not name or not email or "@" not in email:
                    skipped += 1
                    continue
                
                # Case insensitive name/email query
                existing = db.execute(
                    "SELECT * FROM players WHERE LOWER(name)=LOWER(?) OR LOWER(email)=LOWER(?) LIMIT 1",
                    (name, email),
                ).fetchone()
                if existing:
                    if not existing["email"] and existing["name"].lower() == name.lower():
                        db.execute("UPDATE players SET email=? WHERE id=?", (email, existing["id"]))
                        updated += 1
                    else:
                        skipped += 1
                    continue
                
                db.execute("INSERT INTO players(name,email) VALUES(?,?)", (name, email))
                imported += 1
        
        flash(f"Importação concluída: {imported} novos, {updated} atualizados e {skipped} ignorados.", "success")
    except Exception as exc:
        current_app.logger.error(f"Erro ao importar jogadores: {exc}")
        flash(f"Não foi possível importar: {exc}", "danger")
    return redirect(url_for("players.players"))
