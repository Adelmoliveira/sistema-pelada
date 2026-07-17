from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_file
from src.db import get_db
from src.routes.auth import roles_allowed, make_password_hash
from src.utils import alphabetical_key, normalize_cpf, spreadsheet_rows
from src.services.material_photos import process_material_photo
from src.services.players_pdf import build_players_pdf
from src.utils import local_today

bp = Blueprint("players", __name__)


def _player_report_rows(db, query=""):
    rows = db.execute("SELECT * FROM players WHERE active=1").fetchall()
    query = (query or "").strip()
    if query:
        folded = query.casefold()
        rows = [row for row in rows if folded in (row["war_name"] or "").casefold() or folded in (row["name"] or "").casefold()]
    return sorted(rows, key=lambda row: alphabetical_key(row["war_name"] or row["name"]))


@bp.get("/players/report")
@roles_allowed("manager")
def players_report():
    query = request.args.get("q", "").strip()
    all_players = _player_report_rows(get_db(), query)
    per_page = 10
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    pages = max(1, (len(all_players) + per_page - 1) // per_page)
    page = min(page, pages)
    players = all_players[(page - 1) * per_page:page * per_page]
    return render_template("players_report.html", players=players, query=query, page=page, pages=pages, total=len(all_players))


@bp.get("/players/report.pdf")
@roles_allowed("manager")
def players_report_pdf():
    query = request.args.get("q", "").strip()
    report = build_players_pdf(_player_report_rows(get_db(), query), local_today(), query)
    return send_file(report, mimetype="application/pdf", as_attachment=True, download_name="cadastro-completo-peladeiros.pdf")


@bp.get("/players/report/<int:player_id>")
@roles_allowed("manager")
def player_report_detail(player_id):
    player = get_db().execute("SELECT * FROM players WHERE id=? AND active=1", (player_id,)).fetchone()
    if not player:
        flash("Peladeiro não encontrado.", "warning")
        return redirect(url_for("players.players_report"))
    return render_template("player_report_detail.html", player=player)


def _validate_war_name(db, war_name, player_id=None):
    war_name = (war_name or "").strip()
    if not war_name:
        return ""
    query = "SELECT id FROM players WHERE LOWER(war_name)=LOWER(?)"
    params = [war_name]
    if player_id is not None:
        query += " AND id<>?"
        params.append(player_id)
    if db.execute(query, tuple(params)).fetchone():
        raise ValueError("Já existe outro peladeiro com esse nome de guerra.")
    return war_name

@bp.get("/urgent")
@roles_allowed("manager", "staff", "client", "infra", "maintenance")
def urgent():
    db = get_db()
    items = db.execute(
        """SELECT name,war_name,emergency_phone
           FROM players
           WHERE active=1"""
    ).fetchall()
    items = sorted(items, key=lambda player: alphabetical_key(player["name"]))
    return render_template("urgent.html", players=items)

@bp.route("/players", methods=["GET", "POST"])
@roles_allowed("manager")
def players():
    db = get_db()
    if request.method == "POST":
        try:
            membership_type = request.form.get("membership_type", "regular")
            if membership_type not in ("regular", "goalkeeper", "board", "veteran"):
                raise ValueError("Classificação financeira inválida.")
            
            war_name = _validate_war_name(db, request.form.get("war_name", ""))
            processed_photo = process_material_photo(request.files.get("photo"))
            photo_data, thumbnail_data = processed_photo or ("", "")
            db.execute(
                """INSERT INTO players
                (name,war_name,cpf,phone,emergency_phone,email,membership_type,photo_data,thumbnail_data) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    request.form["name"].strip(),
                    war_name,
                    normalize_cpf(request.form.get("cpf")),
                    request.form.get("phone", "").strip(),
                    request.form.get("emergency_phone", "").strip(),
                    request.form.get("email", "").strip().lower(),
                    membership_type, photo_data, thumbnail_data
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
        "veteran": ("active=1 AND membership_type=?", ("veteran",)),
        "inactive": ("active=0", ()),
        "all": ("1=1", ()),
    }
    if player_filter not in filters:
        player_filter = "active"
    where, params = filters[player_filter]
    items = db.execute(f"SELECT * FROM players WHERE {where}", params).fetchall()
    items = sorted(items, key=lambda player: alphabetical_key(player["war_name"] or player["name"]))
    return render_template(
        "players.html",
        players=items,
        player_filter=player_filter,
        players_count=len(items),
    )

@bp.post("/players/<int:player_id>/membership-type")
@roles_allowed("manager")
def player_membership_type(player_id):
    membership_type = request.form.get("membership_type")
    if membership_type not in ("regular", "goalkeeper", "board", "veteran"):
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


@bp.post("/players/<int:player_id>/password")
@roles_allowed("manager")
def reset_player_password(player_id):
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    password = request.form.get("new_password", "")
    if not player:
        flash("Peladeiro não encontrado.", "warning")
    elif not player["war_name"]:
        flash("Cadastre o nome de guerra antes de criar o acesso.", "danger")
    elif len(password) < 8:
        flash("A senha deve ter ao menos 8 caracteres.", "danger")
    else:
        try:
            user = db.execute("SELECT * FROM users WHERE player_id=? OR LOWER(username)=LOWER(?) LIMIT 1",
                             (player_id, player["war_name"])).fetchone()
            if user and user["role"] != "client":
                raise ValueError("O nome de guerra já está vinculado a outro tipo de usuário.")
            if user:
                db.execute("UPDATE users SET username=?,name=?,password_hash=?,password_required=1,player_id=? WHERE id=?",
                           (player["war_name"], player["war_name"], make_password_hash(password), player_id, user["id"]))
            else:
                db.execute("INSERT INTO users(username,name,password_hash,password_required,role,player_id) VALUES(?,?,?,1,'client',?)",
                           (player["war_name"], player["war_name"], make_password_hash(password), player_id))
            db.commit()
            flash(f"Senha de {player['war_name']} atualizada.", "success")
        except ValueError as exc:
            db.rollback()
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao redefinir senha do peladeiro {player_id}: {exc}")
            flash("Não foi possível alterar a senha. Verifique se o nome de guerra já está em uso.", "danger")
    return redirect(url_for("players.edit_player", player_id=player_id))

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
        if membership_type not in ("regular", "goalkeeper", "board", "veteran"):
            flash("Classificação financeira inválida.", "danger")
        else:
            try:
                processed_photo = process_material_photo(request.files.get("photo"))
                photo_fields = (processed_photo or (player["photo_data"], player["thumbnail_data"]))
                db.execute(
                    """UPDATE players SET name=?,war_name=?,cpf=?,email=?,phone=?,emergency_phone=?,membership_type=?,photo_data=?,thumbnail_data=?
                    WHERE id=?""",
                    (
                        request.form["name"].strip(),
                        _validate_war_name(db, request.form.get("war_name", ""), player_id),
                        normalize_cpf(request.form.get("cpf")),
                        request.form.get("email", "").strip().lower(),
                        request.form.get("phone", "").strip(),
                        request.form.get("emergency_phone", "").strip(),
                        membership_type,
                        photo_fields[0], photo_fields[1],
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
        expected_headers = {
            "name": ("nomecompleto", "nome", "name", "peladeiro"),
            "war_name": ("nomedeguerra", "nomeguerra", "apelido"),
            "cpf": ("cpf",),
            "email": ("email", "emailaddress"),
            "phone": ("telefone", "phone", "celular"),
            "emergency_phone": (
                "temergencia",
                "t.emergencia",
                "telefoneemergencia",
                "telefonedeemergencia",
                "contatoemergencia",
            ),
        }
        indexes = {
            field: next((headers[key] for key in aliases if key in headers), None)
            for field, aliases in expected_headers.items()
        }
        if any(index is None for index in indexes.values()):
            raise ValueError(
                "A primeira linha precisa ter as colunas Nome Completo, Nome de Guerra, "
                "CPF, e-mail, Telefone e T. Emergência."
            )
        
        imported = updated = skipped = 0
        db = get_db()
        with db:
            for row in rows[1:]:
                def cell(field):
                    index = indexes[field]
                    value = row[index] if index < len(row) else ""
                    if value is None:
                        return ""
                    # O Excel pode devolver campos numéricos como 123.0.
                    return str(int(value) if isinstance(value, float) and value.is_integer() else value).strip()

                name = cell("name")
                war_name = cell("war_name")
                email = cell("email").lower()
                phone = cell("phone")
                emergency_phone = cell("emergency_phone")
                try:
                    cpf = normalize_cpf(cell("cpf"))
                except ValueError:
                    skipped += 1
                    continue

                if not name or (email and "@" not in email):
                    skipped += 1
                    continue

                existing = db.execute(
                    """SELECT * FROM players
                       WHERE LOWER(name)=LOWER(?)
                          OR (?<>'' AND LOWER(email)=LOWER(?))
                          OR (?<>'' AND cpf=?)
                       ORDER BY CASE WHEN ?<>'' AND cpf=? THEN 0
                                     WHEN ?<>'' AND LOWER(email)=LOWER(?) THEN 1 ELSE 2 END
                       LIMIT 1""",
                    (name, email, email, cpf, cpf, cpf, cpf, email, email),
                ).fetchone()
                if existing:
                    db.execute(
                        """UPDATE players
                           SET name=?,war_name=?,cpf=?,email=?,phone=?,emergency_phone=?
                           WHERE id=?""",
                        (name, war_name, cpf, email, phone, emergency_phone, existing["id"]),
                    )
                    updated += 1
                    continue

                db.execute(
                    """INSERT INTO players(name,war_name,cpf,email,phone,emergency_phone)
                       VALUES(?,?,?,?,?,?)""",
                    (name, war_name, cpf, email, phone, emergency_phone),
                )
                imported += 1
        
        flash(f"Importação concluída: {imported} novos, {updated} atualizados e {skipped} ignorados.", "success")
    except Exception as exc:
        current_app.logger.error(f"Erro ao importar jogadores: {exc}")
        flash(f"Não foi possível importar: {exc}", "danger")
    return redirect(url_for("players.players"))
