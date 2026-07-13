import os
from flask import Flask, g, redirect, request, session, url_for
from flask_wtf.csrf import CSRFProtect

from src.db import get_db
from src.utils import money, brdate, cpfmask
from src.routes.auth import bp as auth_bp
from src.routes.players import bp as players_bp
from src.routes.products import bp as products_bp
from src.routes.sales import bp as sales_bp
from src.routes.finance import bp as finance_bp

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao"),
    DATABASE=os.path.join(app.root_path, "bar.db"),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    PIX_KEY=os.environ.get("PIX_KEY", "adelmoliveira@gmail.com"),
    PIX_MERCHANT_NAME=os.environ.get("PIX_MERCHANT_NAME", "BAR PELADEIROS GPCTA"),
    PIX_MERCHANT_CITY=os.environ.get("PIX_MERCHANT_CITY", "SAO PAULO"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# CSRF Protection
csrf = CSRFProtect(app)

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(players_bp)
app.register_blueprint(products_bp)
app.register_blueprint(sales_bp)
app.register_blueprint(finance_bp)

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

    public_endpoints = {"auth.login", "auth.setup", "static"}
    if request.endpoint in public_endpoints or request.endpoint is None:
        return None

    try:
        # Check if there are any users in the DB
        has_users = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    except Exception as exc:
        app.logger.error(f"Erro ao verificar tabela de usuários: {exc}")
        has_users = None

    if not has_users:
        return redirect(url_for("auth.setup"))

    if not g.user:
        return redirect(url_for("auth.login", next=request.path))

@app.context_processor
def inject_user():
    return {"current_user": g.get("user")}

if __name__ == "__main__":
    app.run(debug=True)
