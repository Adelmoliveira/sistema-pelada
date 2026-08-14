import os
from pathlib import Path
from flask import Flask, g, redirect, request, session, url_for, flash, jsonify, render_template
from flask_wtf.csrf import CSRFProtect, CSRFError

# Carregar variáveis de ambiente do arquivo .env.local se existir (desenvolvimento)
try:
    from dotenv import load_dotenv
    env_file = Path(__file__).parent / ".env.local"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass

from src.db import database_error_category, get_db, is_transient_database_error, read_user_from_session
from src.utils import money, brdate, cpfmask, local_today, month_year_label, service_medals
from src.routes.auth import bp as auth_bp, home_endpoint
from src.routes.players import bp as players_bp
from src.routes.products import bp as products_bp
from src.routes.sales import bp as sales_bp
from src.routes.credits import bp as credits_bp
from src.routes.finance import bp as finance_bp
from src.routes.infra import bp as infra_bp
from src.routes.maintenance import bp as maintenance_bp
from src.routes.cash import bp as cash_bp
from src.routes.display import bp as display_bp
from src.routes.reports import bp as reports_bp
from src.routes.football import bp as football_bp
from src.routes.events import bp as events_bp

app = Flask(__name__)

is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("NOW_REGION"))
database_path = os.environ.get("DATABASE_PATH")
if not database_path:
    database_path = "/tmp/bar.db" if is_vercel else os.path.join(app.root_path, "bar.db")

app.config.update(
    # `or` também cobre variável criada com valor vazio na hospedagem.
    SECRET_KEY=os.environ.get("SECRET_KEY") or "troque-esta-chave-em-producao",
    DATABASE_URL=os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL"),
    DATABASE=database_path,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    PIX_KEY=os.environ.get("PIX_KEY", "adelmoliveira@gmail.com"),
    PIX_MERCHANT_NAME=os.environ.get("PIX_MERCHANT_NAME", "PELADEIROS GPCTA"),
    PIX_MERCHANT_CITY=os.environ.get("PIX_MERCHANT_CITY", "SAO PAULO"),
    MERCADOPAGO_ACCESS_TOKEN=os.environ.get("MERCADOPAGO_ACCESS_TOKEN"),
    MERCADOPAGO_POS_ID=os.environ.get("MERCADOPAGO_POS_ID"),
    MERCADOPAGO_WEBHOOK_SECRET=os.environ.get("MERCADOPAGO_WEBHOOK_SECRET"),
    GMAIL_SMTP_USER=os.environ.get("GMAIL_SMTP_USER"),
    GMAIL_APP_PASSWORD=os.environ.get("GMAIL_APP_PASSWORD"),
    CRON_SECRET=os.environ.get("CRON_SECRET"),
    VAPID_PUBLIC_KEY=os.environ.get("VAPID_PUBLIC_KEY"),
    VAPID_PRIVATE_KEY=os.environ.get("VAPID_PRIVATE_KEY"),
    VAPID_SUBJECT=os.environ.get("VAPID_SUBJECT", "mailto:diretoriagpcta@gmail.com"),
    BAR_CREDIT_LOW_THRESHOLD_CENTS=int(os.environ.get("BAR_CREDIT_LOW_THRESHOLD_CENTS", "1000") or 1000),
    BAR_CREDIT_MAX_TOPUP_CENTS=int(os.environ.get("BAR_CREDIT_MAX_TOPUP_CENTS", "50000") or 50000),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_vercel,
)

if is_vercel:
    app.logger.info(f"[VERCEL] DATABASE_URL configurada: {bool(app.config['DATABASE_URL'])}")
    app.logger.info(f"[VERCEL] SECRET_KEY customizada: {app.config['SECRET_KEY'] != 'troque-esta-chave-em-producao'}")

# CSRF Protection
csrf = CSRFProtect(app)

@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    flash("Sessão expirada ou token inválido. Recarregue a página e tente novamente.", "danger")
    return redirect(request.referrer or url_for("auth.login"), code=303)


@app.errorhandler(413)
def handle_request_too_large(error):
    if request.path.startswith("/infra/load-relation") or request.path.startswith("/infra/loans"):
        message = "O envio deve ter no máximo 4 MB no total. Selecione menos fotos ou use arquivos menores."
    else:
        message = "O arquivo enviado excede o limite permitido. Use um arquivo menor."
    if request.path.endswith("/check-auto") or request.accept_mimetypes.best == "application/json":
        return jsonify(ok=False, error=message), 413
    flash(message, "danger")
    fallback = url_for("infra.load_relation") if request.path.startswith("/infra/load-relation") else url_for("auth.login")
    return redirect(request.referrer or fallback, code=303)


@app.errorhandler(405)
def handle_method_not_allowed(error):
    if g.get("user") and request.method == "POST" and request.accept_mimetypes.accept_html:
        flash("A página anterior estava desatualizada e foi recarregada com segurança.", "warning")
        return redirect(url_for(home_endpoint(g.user["role"])), code=303)
    return error

@app.errorhandler(500)
def handle_internal_error(error):
    original = getattr(error, "original_exception", None) or error
    app.logger.error(
        "DB_QUERY_ERROR function=handle_internal_error operation=REQUEST path=%s "
        "exception_type=%s",
        request.path, type(original).__name__,
    )
    error_msg = str(error)
    if "DATABASE_URL" in error_msg:
        return "Erro: DATABASE_URL não configurada corretamente. Verifique o ambiente Vercel.", 500
    elif "connection" in error_msg.lower() or "psycopg2" in error_msg.lower():
        return "Erro: Não foi possível conectar ao banco de dados Supabase. Verifique DATABASE_URL.", 500
    return "Erro interno no servidor. Tente novamente em alguns momentos.", 500

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(players_bp)
app.register_blueprint(products_bp)
app.register_blueprint(sales_bp)
app.register_blueprint(credits_bp)
app.register_blueprint(finance_bp)
app.register_blueprint(infra_bp)
app.register_blueprint(maintenance_bp)
app.register_blueprint(cash_bp)
app.register_blueprint(display_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(football_bp)
app.register_blueprint(events_bp)


@app.get("/service-worker.js")
def service_worker():
    response = app.send_static_file("service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/offline")
def offline():
    return render_template("offline.html")

# Exempt public/authentication routes from CSRF to avoid login issues in local/dev deployments
from src.routes.auth import setup, login, client_access, logout, push_subscribe, push_unsubscribe
from src.routes.sales import mercadopago_create_order, mercadopago_webhook
from src.routes.credits import purchase as credit_purchase
csrf.exempt(setup)
csrf.exempt(login)
csrf.exempt(client_access)
csrf.exempt(logout)
csrf.exempt(mercadopago_create_order)
csrf.exempt(mercadopago_webhook)
csrf.exempt(credit_purchase)
csrf.exempt(push_subscribe)
csrf.exempt(push_unsubscribe)

# Register Template Filters
app.template_filter("money")(money)
app.template_filter("brdate")(brdate)
app.template_filter("cpfmask")(cpfmask)
app.template_filter("month_year")(month_year_label)

# Security check for default secret key
if not app.debug and app.config["SECRET_KEY"] == "troque-esta-chave-em-producao":
    app.logger.warning("AVISO DE SEGURANÇA: Chave secreta padrão está sendo usada em modo de produção!")

@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        try:
            if _error is not None:
                connection.rollback()
        finally:
            connection.close()


@app.get("/health")
def health():
    """Minimal external health check; never performs setup or migrations."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return jsonify(status="ok", database="ok")
    except Exception as exc:
        db = g.get("db")
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        app.logger.error(
            "DB_HEALTHCHECK_ERROR function=health operation=SELECT_1 path=%s "
            "exception_type=%s category=%s sqlstate=%s",
            request.path, type(exc).__name__, database_error_category(exc),
            getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None) or "-",
        )
        status_code = 503 if is_transient_database_error(exc) else 500
        return jsonify(status="unavailable", database="unavailable"), status_code

@app.before_request
def load_user_and_protect_routes():
    g.user = None

    # A rota valida um token temporário próprio para não depender da cookie de
    # sessão em requisições fetch do Safari/iOS.
    if request.endpoint in {
        "sales.pix_qrcode",
        "sales.mercadopago_create_order",
        "sales.mercadopago_order_status",
        "sales.mercadopago_webhook",
        "finance.payment_reminders_cron",
        "finance.weekly_tribute_cron",
        "football.tribute_image",
    }:
        return None

    # Arquivos do PWA precisam continuar disponíveis mesmo durante uma
    # instabilidade momentânea do banco de dados.
    if request.endpoint in {"static", "service_worker", "offline", "health"}:
        return None

    def database_unavailable(exc, operation):
        db = g.get("db")
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        category = database_error_category(exc)
        app.logger.error(
            "%s function=load_user_and_protect_routes operation=%s path=%s "
            "exception_type=%s category=%s sqlstate=%s",
            "SESSION_LOAD_ERROR" if operation.startswith("carregar") else "DB_HEALTHCHECK_ERROR",
            operation, request.path, type(exc).__name__, category,
            getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None) or "-",
        )
        if not is_transient_database_error(exc):
            raise exc
        message = "Não foi possível conectar ao sistema agora. Sua sessão foi preservada; tente novamente."
        if request.accept_mimetypes.best == "application/json":
            response = jsonify(error=message)
        else:
            response = app.make_response(render_template("service_unavailable.html"))
        response.status_code = 503
        response.headers["Retry-After"] = "3"
        return response

    user_id = session.get("user_id")
    if user_id:
        try:
            # This must remain a read-only operation. In particular, do not
            # update last_login/last_seen or repair schema while loading the
            # signed session cookie.
            g.user = read_user_from_session(user_id)
            if not g.user:
                session.clear()
        except Exception as exc:
            return database_unavailable(exc, "carregar usuário da sessão")

    # Sempre permitir acesso a arquivos estáticos e à rota de setup inicial
    if request.endpoint == "auth.setup":
        return None

    # A successful authenticated-user read already proves that the database
    # and users table are available. Keep the setup probe only before login.
    if not g.user:
        try:
            has_users = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
        except Exception as exc:
            return database_unavailable(exc, "verificar tabela de usuários")

        if not has_users:
            return redirect(url_for("auth.setup"))

    public_endpoints = {"auth.login", "auth.branding_logo", "auth.client_access", "auth.client_password_setup", "auth.forgot_password", "auth.reset_password", "auth.club_card", "auth.club_card_manifest", "sales.guest_event_sale"}
    if request.endpoint in public_endpoints or request.endpoint is None:
        return None

    if not g.user:
        if request.endpoint == "auth.login":
            return None
        if request.accept_mimetypes.best == "application/json":
            return jsonify(error="Sua sessão expirou. Recarregue a página e entre novamente."), 401
        return redirect(url_for("auth.login", next=request.path))

@app.context_processor
def inject_user():
    player = None
    today_birthdays = []
    unread_notifications = 0
    unread_restock_notifications = 0
    pending_restock_requests = 0
    user = g.get("user")
    if user:
        try:
            db = get_db()
            if user["role"] == "client" and user["player_id"]:
                player = db.execute("SELECT name, war_name, thumbnail_data, football_join_date FROM players WHERE id=?", (user["player_id"],)).fetchone()
                unread_notifications = db.execute("SELECT COUNT(*) AS total FROM push_inbox WHERE player_id=? AND read_at IS NULL", (user["player_id"],)).fetchone()["total"]
            if user["role"] == "manager":
                pending_restock_requests = db.execute("SELECT COUNT(*) AS total FROM bar_restock_requests WHERE status='PENDENTE'").fetchone()["total"]
            if user["role"] == "staff":
                unread_restock_notifications = db.execute("SELECT COUNT(*) AS total FROM bar_restock_notifications WHERE user_id=? AND read_at IS NULL", (user["id"],)).fetchone()["total"]
            today_birthdays = db.execute("""SELECT id, name, war_name, gender, thumbnail_data
                FROM players WHERE active=1 AND birth_date<>'' AND substr(birth_date,6,5)=?
                ORDER BY LOWER(COALESCE(war_name, name))""",
                (local_today().strftime("%m-%d"),)).fetchall()
        except Exception:
            player = None
            today_birthdays = []
            unread_notifications = 0
            unread_restock_notifications = 0
            pending_restock_requests = 0
    medals = service_medals(player["football_join_date"]) if player else []
    return {"current_user": user, "current_player": player, "today_birthdays": today_birthdays, "unread_notifications": unread_notifications, "unread_restock_notifications": unread_restock_notifications, "pending_restock_requests": pending_restock_requests, "service_medals": medals}

if __name__ == "__main__":
    app.run(debug=True)
