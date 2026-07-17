import os
from functools import wraps
from urllib.parse import urlsplit
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from src.db import get_db
from src.services.material_photos import process_material_photo

bp = Blueprint("auth", __name__)

def home_endpoint(role):
    if role == "client":
        return "sales.sale"
    if role == "infra":
        return "infra.load_relation"
    if role == "maintenance":
        return "maintenance.new_request"
    return "finance.dashboard"

def safe_next_url(value):
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    try:
        endpoint, _values = current_app.url_map.bind_to_environ(request.environ).match(
            urlsplit(value).path, method="GET"
        )
    except HTTPException:
        return None
    if endpoint in {"auth.login", "auth.logout"}:
        return None
    return value

def make_password_hash(password):
    # Compatível com o Python do macOS e com o ambiente de produção.
    return generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)


def _client_player_for_username(db, username):
    return db.execute(
        "SELECT * FROM players WHERE active=1 AND war_name<>'' AND LOWER(war_name)=LOWER(?)",
        (username.strip(),),
    ).fetchone()


def _client_password_setup(player, user=None):
    return render_template("client_password_setup.html", player=player, existing_user=user)

def roles_allowed(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user or g.user["role"] not in roles:
                if request.accept_mimetypes.best == "application/json":
                    message = "Sua sessão expirou ou seu usuário não possui acesso a esta funcionalidade."
                    return jsonify(error=message), 401 if not g.user else 403
                flash("Seu usuário não possui acesso a essa funcionalidade.", "danger")
                return redirect(url_for(home_endpoint(g.user["role"])))
            return view(*args, **kwargs)
        return wrapped
    return decorator

@bp.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        if len(username) < 3 or len(password) < 8:
            flash("Use um usuário com ao menos 3 caracteres e senha com ao menos 8.", "danger")
        elif password != request.form.get("password_confirm"):
            flash("As senhas não coincidem.", "danger")
        else:
            try:
                db.execute(
                    "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,'manager')",
                    (username, request.form["name"].strip(), make_password_hash(password))
                )
                db.commit()
                flash("Gerente criado. Entre com seu usuário e senha.", "success")
                return redirect(url_for("auth.login"))
            except Exception as exc:
                current_app.logger.error(f"Erro no setup inicial: {exc}")
                flash("Erro interno ao criar gerente de setup.", "danger")
    return render_template("setup.html")

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(
            safe_next_url(request.args.get("next")) or url_for(home_endpoint(g.user["role"])),
            code=303 if request.method == "POST" else 302,
        )
    if request.method == "POST":
        db = get_db()
        username = request.form.get("username", "").strip()
        if not username:
            flash("Informe seu nome de usuário ou nome de guerra.", "danger")
            return render_template("login.html"), 200
        # Case insensitive query for username
        user = db.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?) AND active=1",
            (username,)
        ).fetchone()
        player = _client_player_for_username(db, username)
        if player and (not user or (user["role"] == "client" and not user["password_required"])):
            session["pending_client_player_id"] = player["id"]
            return _client_password_setup(player, user)
        passwordless_user = user and user["role"] == "maintenance" and not user["password_required"]
        if user and (passwordless_user or check_password_hash(user["password_hash"], request.form.get("password", ""))):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(
                safe_next_url(request.form.get("next")) or url_for(home_endpoint(user["role"])),
                code=303,
            )
        flash("Usuário ou senha inválidos.", "danger")
    return render_template("login.html")

@bp.route("/cliente", methods=["GET", "POST"])
def client_access():
    return redirect(url_for("auth.login"))


@bp.route("/cliente/senha", methods=["GET", "POST"])
def client_password_setup():
    player_id = session.get("pending_client_player_id")
    if not player_id:
        return redirect(url_for("auth.login"))
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id=? AND active=1", (player_id,)).fetchone()
    if not player or not player["war_name"]:
        session.pop("pending_client_player_id", None)
        flash("Peladeiro não encontrado ou sem nome de guerra cadastrado.", "danger")
        return redirect(url_for("auth.login"))
    user = db.execute("SELECT * FROM users WHERE player_id=? OR LOWER(username)=LOWER(?) LIMIT 1", (player_id, player["war_name"])).fetchone()
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirm", "")
        if len(password) < 8:
            flash("A senha deve ter ao menos 8 caracteres.", "danger")
        elif password != confirmation:
            flash("As senhas não coincidem.", "danger")
        elif user and user["role"] != "client":
            flash("Este nome de guerra já está vinculado a outro tipo de usuário.", "danger")
        else:
            try:
                password_hash = make_password_hash(password)
                if user:
                    db.execute("UPDATE users SET password_hash=?,password_required=1,player_id=?,name=?,username=? WHERE id=?",
                               (password_hash, player_id, player["war_name"], player["war_name"], user["id"]))
                    user_id = user["id"]
                else:
                    cursor = db.execute("INSERT INTO users(username,name,password_hash,password_required,role,player_id) VALUES(?,?,?,1,'client',?)",
                                        (player["war_name"], player["war_name"], password_hash, player_id))
                    user_id = cursor.lastrowid
                db.commit()
                session.pop("pending_client_player_id", None)
                session.clear()
                session["user_id"] = user_id
                return redirect(url_for("sales.sale"), code=303)
            except Exception as exc:
                db.rollback()
                current_app.logger.error(f"Erro ao configurar senha do peladeiro {player_id}: {exc}")
                flash("Não foi possível configurar sua senha. Tente novamente.", "danger")
    return _client_password_setup(player, user)

@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"), code=303)


@bp.route("/minha-conta", methods=["GET", "POST"])
@roles_allowed("client")
def my_account():
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id=? AND active=1", (g.user["player_id"],)).fetchone()
    if not player:
        flash("Seu usuário ainda não está vinculado a um peladeiro.", "danger")
        return redirect(url_for("sales.sale"))
    if request.method == "POST":
        try:
            processed = process_material_photo(request.files.get("photo"))
            if not processed:
                raise ValueError("Escolha uma foto antes de salvar.")
            photo_data, thumbnail_data = processed
            db.execute("UPDATE players SET photo_data=?,thumbnail_data=? WHERE id=?",
                       (photo_data, thumbnail_data, player["id"]))
            db.commit()
            flash("Foto atualizada com sucesso.", "success")
            player = db.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao atualizar foto do peladeiro {player['id']}: {exc}")
            flash("Não foi possível atualizar a foto.", "danger")
    return render_template("my_account.html", player=player)

@bp.route("/users", methods=["GET", "POST"])
@roles_allowed("manager")
def users():
    db = get_db()
    if request.method == "POST":
        try:
            username = request.form["username"].strip()
            password = request.form.get("password", "")
            role = request.form["role"]
            passwordless = role == "maintenance" or (role == "client" and request.form.get("passwordless") == "1")
            if len(username) < 3:
                raise ValueError("O usuário deve ter ao menos 3 caracteres.")
            if role not in ("manager", "staff", "client", "infra", "maintenance"):
                raise ValueError("Perfil inválido.")
            if not passwordless and len(password) < 8:
                raise ValueError("A senha deve ter ao menos 8 caracteres.")
            password_hash = make_password_hash(password if not passwordless else os.urandom(32).hex())
            db.execute("INSERT INTO users(username,name,password_hash,role,password_required) VALUES(?,?,?,?,?)", (
                username, request.form["name"].strip(), password_hash, role, 0 if passwordless else 1))
            db.commit()
            flash("Usuário criado.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao criar usuário: {exc}")
            if "unique" in str(exc).lower():
                flash("Não foi possível criar o usuário: Já existe um usuário com esse nome.", "danger")
            else:
                flash("Não foi possível criar o usuário devido a um erro interno.", "danger")
        return redirect(url_for("auth.users"))
    
    rows = db.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()
    return render_template("users.html", users=rows)

@bp.post("/users/<int:user_id>/password")
@roles_allowed("manager")
def reset_user_password(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    password = request.form.get("new_password", "")
    if not target:
        flash("Usuário não encontrado.", "warning")
    elif target["role"] not in ("manager", "staff", "client", "infra"):
        flash("Este usuário não utiliza senha redefinível.", "danger")
    elif len(password) < 8:
        flash("A nova senha deve ter ao menos 8 caracteres.", "danger")
    else:
        try:
            db.execute("UPDATE users SET password_hash=?,password_required=1 WHERE id=?",
                         (make_password_hash(password), user_id))
            db.commit()
            flash(f"Senha de {target['name']} alterada.", "success")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao redefinir senha do usuário {user_id}: {exc}")
            flash("Erro interno ao alterar a senha.", "danger")
    return redirect(url_for("auth.users"))

@bp.post("/users/<int:user_id>/edit")
@roles_allowed("manager")
def edit_user(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    name = request.form.get("name", "").strip()
    username = request.form.get("username", "").strip()
    if not target:
        flash("Usuário não encontrado.", "warning")
    elif not name:
        flash("Informe o nome do usuário.", "danger")
    elif len(username) < 3:
        flash("O usuário deve ter ao menos 3 caracteres.", "danger")
    else:
        try:
            db.execute("UPDATE users SET name=?,username=? WHERE id=?", (name, username, user_id))
            db.commit()
            flash("Usuário atualizado.", "success")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao editar usuário {user_id}: {exc}")
            flash("Já existe um usuário com esse nome de acesso." if "unique" in str(exc).lower()
                  else "Erro interno ao editar usuário.", "danger")
    return redirect(url_for("auth.users"))

@bp.post("/users/<int:user_id>/passwordless")
@roles_allowed("manager")
def toggle_client_passwordless(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target or target["role"] != "client":
        flash("Somente clientes podem usar acesso sem senha.", "danger")
    else:
        new_value = 0 if target["password_required"] else 1
        new_password = request.form.get("new_password", "")
        if new_value and len(new_password) < 8:
            flash("Informe uma nova senha de ao menos 8 caracteres para voltar a exigi-la.", "danger")
        else:
            try:
                if new_value:
                    db.execute("UPDATE users SET password_required=1,password_hash=? WHERE id=?",
                                 (make_password_hash(new_password), user_id))
                else:
                    db.execute("UPDATE users SET password_required=0 WHERE id=?", (user_id,))
                db.commit()
                flash("Cliente agora entra sem senha." if not new_value else "Nova senha definida e obrigatória.", "success")
            except Exception as exc:
                db.rollback()
                current_app.logger.error(f"Erro ao alternar passwordless do cliente {user_id}: {exc}")
                flash("Erro interno ao alterar a configuração do cliente.", "danger")
    return redirect(url_for("auth.users"))

@bp.post("/users/<int:user_id>/toggle")
@roles_allowed("manager")
def toggle_user(user_id):
    db = get_db()
    if user_id == g.user["id"]:
        flash("Você não pode desativar o próprio usuário.", "danger")
    else:
        try:
            db.execute("UPDATE users SET active=1-active WHERE id=?", (user_id,))
            db.commit()
            flash("Acesso do usuário atualizado.", "success")
        except Exception as exc:
            current_app.logger.error(f"Erro ao alternar status do usuário {user_id}: {exc}")
            flash("Erro interno ao atualizar acesso do usuário.", "danger")
    return redirect(url_for("auth.users"))
