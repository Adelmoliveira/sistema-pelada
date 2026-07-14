import os
from pathlib import Path
from flask import Flask, g, redirect, request, session, url_for, flash
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
from src.routes.auth import bp as auth_bp
from src.routes.players import bp as players_bp
from src.routes.products import bp as products_bp
from src.routes.sales import bp as sales_bp
from src.routes.finance import bp as finance_bp

app = Flask(__name__)

is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("NOW_REGION"))
database_path = os.environ.get("DATABASE_PATH")
if not database_path:
    database_path = "/tmp/bar.db" if is_vercel else os.path.join(app.root_path, "bar.db")

app.config.update(
    # `or` também cobre variável criada com valor vazio na hospedagem.
    SECRET_KEY=os.environ.get("SECRET_KEY") or os.getenv("SECRET_KEY", "troque-esta-chave-em-producao"),
    DATABASE_URL=os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or os.getenv("DATABASE_URL"),
    DATABASE=database_path,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    PIX_KEY=os.environ.get("PIX_KEY", "adelmoliveira@gmail.com"),
    PIX_MERCHANT_NAME=os.environ.get("PIX_MERCHANT_NAME", "BAR PELADEIROS GPCTA"),
    PIX_MERCHANT_CITY=os.environ.get("PIX_MERCHANT_CITY", "SAO PAULO"),
    MERCADOPAGO_ACCESS_TOKEN=os.environ.get("MERCADOPAGO_ACCESS_TOKEN"),
    MERCADOPAGO_WEBHOOK_SECRET=os.environ.get("MERCADOPAGO_WEBHOOK_SECRET"),
    APP_BASE_URL=(os.environ.get("APP_BASE_URL") or "").rstrip("/"),
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
    return redirect(request.referrer or url_for("auth.login"))

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

# Exempt public/authentication routes from CSRF to avoid login issues in local/dev deployments
from src.routes.auth import setup, login, client_access, logout
from src.routes.sales import mercadopago_webhook
csrf.exempt(setup)
csrf.exempt(login)
csrf.exempt(client_access)
csrf.exempt(logout)
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
    user_id = session.get("user_id")
    if user_id:
        try:
            g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
            if not g.user:
                session.clear()
        except Exception as exc:
            app.logger.error(f"Erro ao carregar usuário da sessão: {exc}")
            session.clear()

    # Sempre permitir acesso a arquivos estáticos e à rota de setup inicial
    if request.endpoint == "static" or request.endpoint == "auth.setup":
        return None

    try:
        has_users = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    except Exception as exc:
        app.logger.error(f"Erro ao verificar tabela de usuários: {exc}")
        has_users = None

    if not has_users:
        if request.endpoint == "auth.setup":
            return None
        return redirect(url_for("auth.setup"))

    public_endpoints = {"auth.login", "auth.client_access", "sales.mercadopago_webhook"}
    if request.endpoint in public_endpoints or request.endpoint is None:
        return None

    if not g.user:
        if request.endpoint == "auth.login":
            return None
        return redirect(url_for("auth.login", next=request.path))

@app.context_processor
def inject_user():
    return {"current_user": g.get("user")}

if __name__ == "__main__":
    app.run(debug=True)
