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

from src.db import get_db
from src.utils import money, brdate, cpfmask
from src.routes.auth import bp as auth_bp, home_endpoint
from src.routes.players import bp as players_bp
from src.routes.products import bp as products_bp
from src.routes.sales import bp as sales_bp
from src.routes.finance import bp as finance_bp
from src.routes.infra import bp as infra_bp
from src.routes.maintenance import bp as maintenance_bp
from src.routes.cash import bp as cash_bp

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

@app.errorhandler(405)
def handle_method_not_allowed(error):
    if g.get("user") and request.method == "POST" and request.accept_mimetypes.accept_html:
        flash("A página anterior estava desatualizada e foi recarregada com segurança.", "warning")
        return redirect(url_for(home_endpoint(g.user["role"])), code=303)
    return error

@app.errorhandler(500)
def handle_internal_error(error):
    app.logger.error(f"Erro interno: {error}")
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
app.register_blueprint(finance_bp)
app.register_blueprint(infra_bp)
app.register_blueprint(maintenance_bp)
app.register_blueprint(cash_bp)


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
from src.routes.auth import setup, login, client_access, logout
from src.routes.sales import mercadopago_create_order, mercadopago_webhook
csrf.exempt(setup)
csrf.exempt(login)
csrf.exempt(client_access)
csrf.exempt(logout)
csrf.exempt(mercadopago_create_order)
csrf.exempt(mercadopago_webhook)

# Register Template Filters
app.template_filter("money")(money)
app.template_filter("brdate")(brdate)
app.template_filter("cpfmask")(cpfmask)

# Security check for default secret key
if not app.debug and app.config["SECRET_KEY"] == "troque-esta-chave-em-producao":
    app.logger.warning("AVISO DE SEGURANÇA: Chave secreta padrão está sendo usada em modo de produção!")

@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()

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
    }:
        return None

    # Arquivos do PWA precisam continuar disponíveis mesmo durante uma
    # instabilidade momentânea do banco de dados.
    if request.endpoint in {"static", "service_worker", "offline"}:
        return None

    def database_unavailable(exc, operation):
        app.logger.error(f"Erro ao {operation}: {exc}")
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
            g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
            if not g.user:
                session.clear()
        except Exception as exc:
            return database_unavailable(exc, "carregar usuário da sessão")

    # Sempre permitir acesso a arquivos estáticos e à rota de setup inicial
    if request.endpoint == "auth.setup":
        return None

    try:
        has_users = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    except Exception as exc:
        return database_unavailable(exc, "verificar tabela de usuários")

    if not has_users:
        if request.endpoint == "auth.setup":
            return None
        return redirect(url_for("auth.setup"))

    public_endpoints = {"auth.login", "auth.client_access", "auth.client_password_setup"}
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
    user = g.get("user")
    if user and user["role"] == "client" and user["player_id"]:
        try:
            player = get_db().execute("SELECT thumbnail_data FROM players WHERE id=?", (user["player_id"],)).fetchone()
        except Exception:
            player = None
    return {"current_user": user, "current_player": player}

if __name__ == "__main__":
    app.run(debug=True)
