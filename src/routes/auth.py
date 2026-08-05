import os
import hashlib
import secrets
import base64
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlsplit
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app, jsonify, Response, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from src.db import get_db
from src.services.material_photos import process_material_photo
from src.utils import local_today, service_medals

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
        return "football.sumulas"
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
        value = str(join_date).strip()
        joined = date.fromisoformat(value + "-01" if len(value) == 7 else value[:10])
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


@bp.get("/branding/logo")
def branding_logo():
    """Serve a manager-selected logo, falling back to the standard GPCTA logo."""
    try:
        row = get_db().execute("SELECT value FROM app_settings WHERE key=?", ("branding_logo_data",)).fetchone()
        data = row["value"] if row and row["value"] else ""
        if data.startswith("data:") and "," in data:
            header, payload = data.split(",", 1)
            mime = header[5:].split(";", 1)[0] or "image/jpeg"
            response = Response(base64.b64decode(payload), mimetype=mime)
            # A logo pode ser trocada pelo gerente; não mantenha uma versão
            # antiga no navegador após o upload.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            return response
    except Exception as exc:
        current_app.logger.warning("Logo personalizada indisponível; usando padrão: %s", exc)
    # Keep compatibility with the Flask versions used locally and on Vercel;
    # older ``send_from_directory`` releases do not accept ``max_age``.
    response = send_from_directory(current_app.static_folder, "logo-gpcta.jpeg")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response

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


@bp.route("/esqueci-senha", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        # Always show the same response so the page does not reveal whether an
        # address is registered. Only client accounts can use this flow.
        if "@" in email:
            db = get_db()
            account = db.execute(
                """SELECT u.id,p.name,p.war_name,p.email FROM users u
                   JOIN players p ON p.id=u.player_id
                   WHERE u.role='client' AND u.active=1 AND p.active=1 AND LOWER(p.email)=LOWER(?)""",
                (email,),
            ).fetchone()
            if account:
                raw_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat(sep=" ")
                try:
                    db.execute("UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP WHERE user_id=? AND used_at IS NULL", (account["id"],))
                    db.execute("INSERT INTO password_reset_tokens(user_id,token_hash,expires_at) VALUES(?,?,?)", (account["id"], token_hash, expires_at))
                    db.commit()
                    from src.services.email_reminders import send_gmail_html
                    sender = current_app.config.get("GMAIL_SMTP_USER") or ""
                    app_password = current_app.config.get("GMAIL_APP_PASSWORD") or ""
                    link = url_for("auth.reset_password", token=raw_token, _external=True)
                    name = account["war_name"] or account["name"]
                    plain = f"Olá, {name}!\n\nRecebemos um pedido para trocar sua senha no PELADEIROS GPCTA.\n\nAcesse o link (válido por 1 hora):\n{link}\n\nSe você não solicitou essa troca, ignore este e-mail."
                    html = f"""<div style='font-family:Arial,sans-serif;color:#183247;line-height:1.55'><h2 style='color:#07558c'>Troca de senha · PELADEIROS GPCTA</h2><p>Olá, {name}!</p><p>Recebemos um pedido para trocar sua senha.</p><p><a href='{link}' style='display:inline-block;background:#07558c;color:#fff;padding:12px 20px;border-radius:6px;text-decoration:none'>Trocar minha senha</a></p><p>O link é válido por 1 hora. Se você não solicitou essa troca, ignore este e-mail.</p></div>"""
                    if sender and app_password:
                        send_gmail_html(sender, app_password, email, "Troca de senha · PELADEIROS GPCTA", plain, html)
                except Exception as exc:
                    db.rollback()
                    current_app.logger.error(f"Erro ao enviar recuperação de senha: {exc}")
        flash("Se o e-mail estiver cadastrado, você receberá um link para trocar a senha.", "info")
        return redirect(url_for("auth.forgot_password"), code=303)
    return render_template("forgot_password.html")


@bp.route("/redefinir-senha", methods=["GET", "POST"])
def reset_password():
    token = request.values.get("token", "").strip()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
    db = get_db()
    reset = db.execute(
        """SELECT t.*,u.id user_id FROM password_reset_tokens t JOIN users u ON u.id=t.user_id
           WHERE t.token_hash=? AND t.used_at IS NULL AND u.active=1""", (token_hash,)
    ).fetchone() if token_hash else None
    valid = False
    if reset:
        try:
            expiry = datetime.fromisoformat(str(reset["expires_at"]).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            valid = expiry > datetime.now(timezone.utc)
        except ValueError:
            valid = False
    if not valid:
        return render_template("reset_password.html", token=token, invalid=True), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirm", "")
        if len(password) < 8:
            flash("A nova senha deve ter ao menos 8 caracteres.", "danger")
        elif password != confirmation:
            flash("As senhas não coincidem.", "danger")
        else:
            db.execute("UPDATE users SET password_hash=?,password_required=1 WHERE id=?", (make_password_hash(password), reset["user_id"]))
            db.execute("UPDATE password_reset_tokens SET used_at=CURRENT_TIMESTAMP WHERE id=?", (reset["id"],))
            db.commit()
            flash("Senha alterada com sucesso. Entre com sua nova senha.", "success")
            return redirect(url_for("auth.login"), code=303)
    return render_template("reset_password.html", token=token, invalid=False)

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
@roles_allowed("client", "manager")
def my_account():
    db = get_db()
    if g.user["role"] == "manager":
        try:
            row = db.execute("SELECT value FROM app_settings WHERE key=?", ("branding_logo_data",)).fetchone()
        except Exception as exc:
            # Older databases may not have received the explicit app_settings
            # migration yet; keep the account page usable with the default logo.
            db.rollback()
            current_app.logger.warning("Configuração de logo ainda não migrada: %s", exc)
            row = None
        return render_template("my_account.html", player=None, branding_logo_active=bool(row and row["value"]))
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
                    parsed_join_date = date.fromisoformat(
                        football_join_date + "-01" if len(football_join_date) == 7 else football_join_date
                    )
                except ValueError:
                    raise ValueError("Informe uma data de apresentação válida.")
                if parsed_join_date > local_today() or parsed_join_date.year < 1900:
                    raise ValueError("A data de apresentação informada não é válida.")
                football_join_date = parsed_join_date.isoformat()

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
        service_medals=service_medals(player["football_join_date"]),
        push_enabled=push_enabled,
    )


@bp.post("/minha-conta/logo")
@roles_allowed("manager")
def update_branding_logo():
    db = get_db()
    try:
        if request.form.get("action") == "reset":
            db.execute("DELETE FROM app_settings WHERE key=?", ("branding_logo_data",))
            db.commit()
            flash("Logo padrão restaurada.", "success")
            return redirect(url_for("auth.my_account"))
        upload = request.files.get("logo")
        if not upload or not upload.filename:
            raise ValueError("Selecione uma imagem para enviar.")
        processed = process_material_photo(upload)
        if not processed:
            raise ValueError("A imagem escolhida não é válida. Use JPG, PNG ou WebP de até 4 MB.")
        logo_data, _thumbnail = processed
        # ``DbWrapper`` adds ``RETURNING id`` to INSERTs for legacy tables.
        # app_settings is keyed by ``key`` and intentionally has no numeric id,
        # so provide the correct RETURNING clause explicitly for both SQLite
        # and PostgreSQL.
        db.execute("""INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            RETURNING key""",
            ("branding_logo_data", logo_data))
        db.commit()
        flash("Logo comemorativa atualizada com sucesso.", "success")
    except ValueError as exc:
        db.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        db.rollback()
        current_app.logger.exception("Erro ao atualizar logo institucional: %s", exc)
        flash("Não foi possível atualizar a logo.", "danger")
    return redirect(url_for("auth.my_account"))


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
        # Registros antigos marcavam o pedido como entregue antes da retirada
        # parcial passar a ser registrada em ``sale_item_deliveries``. Quando
        # não há nenhum detalhe de retirada, ``delivered_at`` é a fonte
        # confiável e todos os itens devem ser exibidos como entregues.
        legacy_delivered_sales = {
            sale["id"] for sale in sales
            if sale.get("delivered_at")
            and not any(
                int(item["delivered_quantity"] or 0) > 0
                for item in item_rows
                if item["sale_id"] == sale["id"]
            )
        }
        if legacy_delivered_sales:
            item_rows = [
                dict(item, delivered_quantity=item["quantity"])
                if item["sale_id"] in legacy_delivered_sales else item
                for item in item_rows
            ]
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


@bp.get("/destaques")
@roles_allowed("client")
def highlights():
    """Show active peladeiros who have earned a service medal."""
    db = get_db()
    players = db.execute(
        """SELECT id, name, war_name, football_join_date, thumbnail_data
           FROM players
           WHERE active=1 AND football_join_date<>''
           ORDER BY LOWER(COALESCE(war_name, name))"""
    ).fetchall()
    featured = []
    for player in players:
        medals = service_medals(player["football_join_date"])
        if medals:
            featured.append({
                "id": player["id"],
                "name": player["name"], "war_name": player["war_name"],
                "football_join_date": player["football_join_date"],
                "thumbnail_data": player["thumbnail_data"], "medals": medals,
            })
    featured.sort(key=lambda player: (-len(player["medals"]), (player["war_name"] or player["name"]).casefold()))
    return render_template("highlights.html", players=featured)


@bp.get("/destaques/<int:player_id>/cartao")
@roles_allowed("client")
def highlight_card(player_id):
    """Render a print-ready recognition card for a medalist."""
    db = get_db()
    player = db.execute(
        """SELECT id, name, war_name, football_join_date, thumbnail_data
           FROM players WHERE id=? AND active=1""",
        (player_id,),
    ).fetchone()
    medals = service_medals(player["football_join_date"]) if player else []
    if not player or not medals:
        flash("Este peladeiro ainda não possui uma condecoração por tempo de grupo.", "warning")
        return redirect(url_for("auth.highlights"))
    return render_template("highlight_card.html", player=player, medals=medals)

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
    # Keep compatibility with an already-open edit form from before the role
    # selector was added.
    role = request.form.get("role", target["role"] if target else "").strip()
    valid_roles = ("manager", "staff", "client", "infra", "maintenance", "display", "football_manager")
    if not target:
        flash("Usuário não encontrado.", "warning")
    elif not name:
        flash("Informe o nome do usuário.", "danger")
    elif len(username) < 3:
        flash("O usuário deve ter ao menos 3 caracteres.", "danger")
    elif role not in valid_roles:
        flash("Perfil inválido.", "danger")
    elif user_id == g.user["id"] and role != target["role"]:
        flash("Não é possível alterar o próprio perfil durante a sessão.", "danger")
    else:
        try:
            # Maintenance and display accounts are intentionally passwordless.
            # Changing another account's profile must also keep that invariant.
            password_required = 0 if role in ("maintenance", "display") else int(target["password_required"] or 0)
            db.execute("UPDATE users SET name=?,username=?,role=?,password_required=? WHERE id=?",
                       (name, username, role, password_required, user_id))
            db.commit()
            flash("Usuário e perfil atualizados.", "success")
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
