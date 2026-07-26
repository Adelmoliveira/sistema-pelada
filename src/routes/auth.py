import os
from datetime import date
from functools import wraps
from urllib.parse import urlsplit
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from src.db import get_db
from src.services.material_photos import process_material_photo
from src.utils import local_today

bp = Blueprint("auth", __name__)

def home_endpoint(role):
    if role == "client":
        return "sales.sale"
    if role == "infra":
        return "infra.load_relation"
    if role == "maintenance":
        return "maintenance.new_request"
    if role == "display":
        return "display.panel"
    if role == "football_manager":
        return "football.dashboard"
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


def _completed_years(join_date):
    """Return complete years since the player's presentation date."""
    if not join_date:
        return None
    try:
        joined = date.fromisoformat(str(join_date)[:10])
    except (TypeError, ValueError):
        return None
    today = local_today()
    years = today.year - joined.year - ((today.month, today.day) < (joined.month, joined.day))
    return max(0, years)


def _client_player_for_username(db, username):
    return db.execute(
        "SELECT * FROM players WHERE active=1 AND war_name<>'' AND LOWER(war_name)=LOWER(?)",
        (username.strip(),),
    ).fetchone()


def _client_password_setup(player, user=None):
    return render_template("client_password_setup.html", player=player, existing_user=user)


def _client_profile_complete(db, player_id):
    player = db.execute(
        "SELECT birth_date, football_join_date, phone, emergency_phone, postal_code FROM players WHERE id=? AND active=1",
        (player_id,),
    ).fetchone()
    if not player:
        return False
    postal_code = "".join(ch for ch in (player["postal_code"] or "") if ch.isdigit())
    return bool(player["birth_date"] and player["football_join_date"] and player["phone"] and player["emergency_phone"] and len(postal_code) == 8)


def _client_home_redirect(db, user):
    if user["role"] == "client" and user["player_id"] and not _client_profile_complete(db, user["player_id"]):
        flash("Complete seu cadastro para continuar.", "info")
        return url_for("auth.my_account")
    return url_for(home_endpoint(user["role"]))

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
            safe_next_url(request.args.get("next")) or _client_home_redirect(get_db(), g.user),
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
        passwordless_user = user and user["role"] in ("maintenance", "display") and not user["password_required"]
        if user and (passwordless_user or check_password_hash(user["password_hash"], request.form.get("password", ""))):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(
                safe_next_url(request.form.get("next")) or _client_home_redirect(db, user),
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
                destination = url_for("auth.my_account")
                flash("Senha criada. Complete seu cadastro para continuar.", "info")
                return redirect(destination, code=303)
            except Exception as exc:
                db.rollback()
                current_app.logger.error(f"Erro ao configurar senha do peladeiro {player_id}: {exc}")
                flash("Não foi possível configurar sua senha. Tente novamente.", "danger")
    return _client_password_setup(player, user)

@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"), code=303)


@bp.get("/notifications/push/public-key")
@roles_allowed("client")
def push_public_key():
    from src.services.push_notifications import public_key
    return jsonify(publicKey=public_key())


@bp.get("/notifications/push/unread-count")
@roles_allowed("client")
def push_unread_count():
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) AS total FROM push_inbox WHERE player_id=? AND read_at IS NULL",
        (g.user["player_id"],),
    ).fetchone()["total"]
    return jsonify(count=int(count or 0))


@bp.post("/notifications/push/subscribe")
@roles_allowed("client")
def push_subscribe():
    subscription = request.get_json(silent=True) or {}
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth_key = str(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth_key or not g.user["player_id"]:
        return jsonify(error="Assinatura inválida."), 400
    db = get_db()
    db.execute("""INSERT INTO push_subscriptions(player_id,endpoint,p256dh,auth) VALUES(?,?,?,?)
        ON CONFLICT(endpoint) DO UPDATE SET player_id=?,p256dh=?,auth=?,updated_at=CURRENT_TIMESTAMP""",
        (g.user["player_id"], endpoint, p256dh, auth_key, g.user["player_id"], p256dh, auth_key))
    db.commit()
    return jsonify(ok=True)


@bp.post("/notifications/push/unsubscribe")
@roles_allowed("client")
def push_unsubscribe():
    subscription = request.get_json(silent=True) or {}
    endpoint = str(subscription.get("endpoint") or "").strip()
    if endpoint:
        db = get_db()
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND player_id=?", (endpoint, g.user["player_id"]))
        db.commit()
    return jsonify(ok=True)


@bp.get("/notificacoes")
@roles_allowed("client")
def notifications_inbox():
    db = get_db()
    messages = db.execute(
        "SELECT * FROM push_inbox WHERE player_id=? ORDER BY id DESC LIMIT 50",
        (g.user["player_id"],),
    ).fetchall()
    db.execute("UPDATE push_inbox SET read_at=CURRENT_TIMESTAMP WHERE player_id=? AND read_at IS NULL", (g.user["player_id"],))
    db.commit()
    return render_template("notifications_inbox.html", messages=messages)


@bp.post("/notificacoes/limpar")
@roles_allowed("client")
def clear_notifications_inbox():
    db = get_db()
    db.execute("DELETE FROM push_inbox WHERE player_id=?", (g.user["player_id"],))
    db.commit()
    flash("Avisos removidos deste dispositivo.", "success")
    return redirect(url_for("auth.notifications_inbox"))


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
            photo_data = player["photo_data"] or ""
            thumbnail_data = player["thumbnail_data"] or ""
            uploaded_photo = request.files.get("photo")
            if uploaded_photo and uploaded_photo.filename:
                processed = process_material_photo(uploaded_photo)
                if not processed:
                    raise ValueError("A foto escolhida não é válida.")
                photo_data, thumbnail_data = processed

            birth_date = request.form.get("birth_date", "").strip()
            if birth_date:
                try:
                    parsed_birth_date = date.fromisoformat(birth_date)
                except ValueError:
                    raise ValueError("Informe uma data de nascimento válida.")
                if parsed_birth_date > local_today() or parsed_birth_date.year < 1900:
                    raise ValueError("A data de nascimento informada não é válida.")

            football_join_date = request.form.get("football_join_date", player["football_join_date"] or "").strip()
            # Formulários legados podem não enviar esse campo; nesse caso preservamos o valor
            # existente. Quando o campo é apresentado, ele é obrigatório e validado.
            if "football_join_date" in request.form:
                if not football_join_date:
                    raise ValueError("A data de apresentação na pelada é obrigatória.")
                try:
                    parsed_join_date = date.fromisoformat(football_join_date)
                except ValueError:
                    raise ValueError("Informe uma data de apresentação válida.")
                if parsed_join_date > local_today() or parsed_join_date.year < 1900:
                    raise ValueError("A data de apresentação informada não é válida.")

            postal_code = "".join(ch for ch in request.form.get("postal_code", "") if ch.isdigit())
            if not birth_date:
                raise ValueError("A data de nascimento é obrigatória.")
            if not request.form.get("phone", "").strip():
                raise ValueError("O contato normal é obrigatório.")
            if not request.form.get("emergency_phone", "").strip():
                raise ValueError("O contato de emergência é obrigatório.")
            if len(postal_code) != 8:
                raise ValueError("O CEP é obrigatório e deve ter 8 dígitos.")
            values = {
                "birth_date": birth_date,
                "football_join_date": football_join_date,
                "phone": request.form.get("phone", "").strip()[:40],
                "emergency_phone": request.form.get("emergency_phone", "").strip()[:40],
                "postal_code": postal_code,
                "address_street": request.form.get("address_street", "").strip()[:160],
                "address_number": request.form.get("address_number", "").strip()[:30],
                "address_complement": request.form.get("address_complement", "").strip()[:100],
                "address_neighborhood": request.form.get("address_neighborhood", "").strip()[:100],
                "address_city": request.form.get("address_city", "").strip()[:100],
                "address_state": request.form.get("address_state", "").strip().upper()[:2],
            }
            db.execute("""UPDATE players SET photo_data=?,thumbnail_data=?,birth_date=?,football_join_date=?,phone=?,
                emergency_phone=?,postal_code=?,address_street=?,address_number=?,address_complement=?,
                address_neighborhood=?,address_city=?,address_state=? WHERE id=?""",
                (photo_data, thumbnail_data, values["birth_date"], values["football_join_date"], values["phone"], values["emergency_phone"],
                 values["postal_code"], values["address_street"], values["address_number"], values["address_complement"],
                 values["address_neighborhood"], values["address_city"], values["address_state"], player["id"]))
            db.commit()
            flash("Foto atualizada com sucesso." if uploaded_photo and uploaded_photo.filename else "Dados da conta atualizados com sucesso.", "success")
            player = db.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            db.rollback()
            current_app.logger.error(f"Erro ao atualizar conta do peladeiro {player['id']}: {exc}")
            flash("Não foi possível atualizar os dados da conta.", "danger")
    push_enabled = bool(db.execute("SELECT 1 FROM push_subscriptions WHERE player_id=? LIMIT 1", (player["id"],)).fetchone())
    return render_template(
        "my_account.html",
        player=player,
        tenure_years=_completed_years(player["football_join_date"]),
        push_enabled=push_enabled,
    )


@bp.get("/minhas-compras")
@roles_allowed("client")
def my_purchases():
    """Show the peladeiro's complete purchase history and pending pickups."""
    db = get_db()
    player_id = g.user["player_id"]
    total_consumed_cents = db.execute(
        "SELECT COALESCE(SUM(total_cents), 0) total_cents FROM sales WHERE player_id=? AND paid=1",
        (player_id,),
    ).fetchone()["total_cents"]
    rows = db.execute(
        """SELECT s.id,s.total_cents,s.payment_method,s.paid,s.payment_status,
                  s.paid_at,s.ready_for_delivery,s.delivered_at,s.created_at
           FROM sales s
           WHERE s.player_id=?
           ORDER BY COALESCE(s.paid_at,s.created_at) DESC,s.id DESC
           LIMIT 50""",
        (player_id,),
    ).fetchall()
    sales = []
    for row in rows:
        sale = dict(row)
        payment_status = (sale.get("payment_status") or "").lower()
        if not sale.get("paid") and payment_status in {"pending", "pending_cash", "creating"}:
            sale["display_status"] = "AGUARDANDO_PAGAMENTO"
            sale["display_status_label"] = "Aguardando pagamento"
            sale["display_status_class"] = "warning"
        elif sale.get("delivered_at"):
            sale["display_status"] = "ENTREGUE"
            sale["display_status_label"] = "Entregue"
            sale["display_status_class"] = "secondary"
        elif sale.get("ready_for_delivery"):
            sale["display_status"] = "AGUARDANDO_RETIRADA"
            sale["display_status_label"] = "Pago · aguardando retirada"
            sale["display_status_class"] = "success"
        else:
            sale["display_status"] = "REGISTRADO"
            sale["display_status_label"] = "Pedido registrado"
            sale["display_status_class"] = "primary"
        sales.append(sale)

    if sales:
        placeholders = ",".join("?" for _ in sales)
        item_rows = db.execute(
            f"""SELECT i.sale_id,i.id item_id,i.quantity,p.name product_name,
                       COALESCE((SELECT SUM(sid.quantity) FROM sale_item_deliveries sid WHERE sid.sale_item_id=i.id),0) delivered_quantity
                FROM sale_items i JOIN products p ON p.id=i.product_id
                WHERE i.sale_id IN ({placeholders}) ORDER BY i.sale_id,i.id""",
            tuple(sale["id"] for sale in sales),
        ).fetchall()
        item_summary = {}
        for item in item_rows:
            delivered = int(item["delivered_quantity"] or 0)
            total = int(item["quantity"] or 0)
            pending = max(0, total - delivered)
            label = f"{item['product_name']} · {delivered}/{total} entregue(s)"
            if pending:
                label += f" · restam {pending}"
            item_summary.setdefault(item["sale_id"], []).append(label)
        for sale in sales:
            sale["items_summary"] = " · ".join(item_summary.get(sale["id"], []))
            sale_items = [item for item in item_rows if item["sale_id"] == sale["id"]]
            sale["delivered_quantity"] = sum(int(item["delivered_quantity"] or 0) for item in sale_items)
            sale["pending_quantity"] = sum(max(0, int(item["quantity"] or 0) - int(item["delivered_quantity"] or 0)) for item in sale_items)
            if sale["delivered_quantity"] and sale["pending_quantity"]:
                sale["display_status"] = "PARCIAL"
                sale["display_status_label"] = f"Parcial · restam {sale['pending_quantity']}"
                sale["display_status_class"] = "warning"

    pending_pickups = [
        sale for sale in sales
        if sale["display_status"] in ("AGUARDANDO_RETIRADA", "PARCIAL")
    ]
    return render_template(
        "my_purchases.html",
        sales=sales,
        pending_pickups=pending_pickups,
        total_consumed_cents=int(total_consumed_cents or 0),
    )


@bp.post("/minha-conta/senha")
@roles_allowed("client")
def change_my_password():
    password = request.form.get("password", "")
    confirmation = request.form.get("password_confirm", "")
    if len(password) < 8:
        flash("A nova senha deve ter ao menos 8 caracteres.", "danger")
    elif password != confirmation:
        flash("As senhas não coincidem.", "danger")
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash=?,password_required=1 WHERE id=?",
                   (make_password_hash(password), g.user["id"]))
        db.commit()
        flash("Senha alterada com sucesso.", "success")
    return redirect(url_for("auth.my_account"))


@bp.get("/aniversariantes")
@roles_allowed("client", "manager")
def birthdays():
    today = local_today()
    db = get_db()
    players = db.execute(
        """SELECT name, war_name, birth_date, thumbnail_data
           FROM players
           WHERE active=1 AND birth_date<>'' AND substr(birth_date, 6, 2)=?
           ORDER BY substr(birth_date, 9, 2), LOWER(COALESCE(war_name, name))""",
        (f"{today.month:02d}",),
    ).fetchall()
    months = ("janeiro", "fevereiro", "março", "abril", "maio", "junho",
              "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")
    return render_template("birthdays.html", players=players, month_name=months[today.month - 1])

@bp.route("/users", methods=["GET", "POST"])
@roles_allowed("manager")
def users():
    db = get_db()
    if request.method == "POST":
        try:
            username = request.form["username"].strip()
            password = request.form.get("password", "")
            role = request.form["role"]
            passwordless = role in ("maintenance", "display") or (role == "client" and request.form.get("passwordless") == "1")
            if len(username) < 3:
                raise ValueError("O usuário deve ter ao menos 3 caracteres.")
            if role not in ("manager", "staff", "client", "infra", "maintenance", "display", "football_manager"):
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
    
    rows = db.execute(
        """SELECT u.*,
                  CASE WHEN u.role='client' AND u.player_id IS NOT NULL
                       THEN COALESCE(p.name, u.name)
                       ELSE u.name END AS display_name
           FROM users u
           LEFT JOIN players p ON p.id=u.player_id
           ORDER BY u.active DESC, display_name"""
    ).fetchall()
    return render_template("users.html", users=rows)

@bp.post("/users/<int:user_id>/password")
@roles_allowed("manager")
def reset_user_password(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    password = request.form.get("new_password", "")
    if not target:
        flash("Usuário não encontrado.", "warning")
    elif target["role"] not in ("manager", "staff", "client", "infra", "football_manager"):
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
