from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from src.db import get_db

bp = Blueprint("auth", __name__)

def roles_allowed(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user or g.user["role"] not in roles:
                flash("Seu usuário não possui acesso a essa funcionalidade.", "danger")
                return redirect(url_for("sales.sale") if g.user and g.user["role"] == "client" else url_for("finance.dashboard"))
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
                    (username, request.form["name"].strip(), generate_password_hash(password))
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
        return redirect(url_for("sales.sale") if g.user["role"] == "client" else url_for("finance.dashboard"))
    if request.method == "POST":
        db = get_db()
        # Case insensitive query for username
        user = db.execute(
            "SELECT * FROM users WHERE LOWER(username)=LOWER(?) AND active=1",
            (request.form["username"].strip(),)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("sales.sale") if user["role"] == "client" else url_for("finance.dashboard"))
        flash("Usuário ou senha inválidos.", "danger")
    return render_template("login.html")

@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

@bp.route("/users", methods=["GET", "POST"])
@roles_allowed("manager")
def users():
    db = get_db()
    if request.method == "POST":
        try:
            username = request.form["username"].strip()
            password = request.form["password"]
            if len(username) < 3 or len(password) < 8:
                raise ValueError("Usuário deve ter 3 caracteres e senha ao menos 8.")
            
            db.execute(
                "INSERT INTO users(username,name,password_hash,role) VALUES(?,?,?,?)",
                (username, request.form["name"].strip(), generate_password_hash(password), request.form["role"])
            )
            db.commit()
            flash("Usuário criado.", "success")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao criar usuário: {exc}")
            if "unique" in str(exc).lower():
                flash("Não foi possível criar o usuário: Já existe um usuário com esse nome.", "danger")
            else:
                flash("Não foi possível criar o usuário devido a um erro interno.", "danger")
        return redirect(url_for("auth.users"))
    
    rows = db.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()
    return render_template("users.html", users=rows)

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
