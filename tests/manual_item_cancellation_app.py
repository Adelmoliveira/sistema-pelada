"""Disposable synthetic app for human mobile/PWA inspection; never a production entrypoint.

PYTHONPATH=tests:. .venv/bin/python tests/manual_item_cancellation_app.py
Then open http://127.0.0.1:5057/login in a local browser.
Ctrl-C removes the temporary database. Restart for pristine orders.
"""
import os

# Set before importing app/fixtures; never inherit a production database URL.
os.environ['DATABASE_URL']=''
os.environ['SUPABASE_DB_URL']=''
os.environ['APP_ENV']='local'


def main():
    from src.routes.auth import make_password_hash
    from test_item_cancellations import ItemCancellationTest
    from app import app
    from src.db import get_db

    fixture=ItemCancellationTest(methodName='runTest')
    fixture.setUp()
    try:
        first=fixture.order(quantity=1)
        with app.app_context():
            db=get_db()
            db.execute("UPDATE products SET name='Cerveja sintética 1' WHERE name='Cerveja'")
            db.commit()
        second=fixture.order(quantity=3)
        app.config.update(DATABASE_URL=None,APP_ENV='local',EXTERNAL_PAYMENTS_ENABLED=False,
                          GMAIL_SMTP_USER='',GMAIL_APP_PASSWORD='',WTF_CSRF_ENABLED=True,
                          SESSION_COOKIE_SECURE=False,MERCADOPAGO_ACCESS_TOKEN='',MERCADOPAGO_WEBHOOK_SECRET='')
        with app.app_context():
            db=get_db()
            db.execute("UPDATE users SET password_hash=? WHERE id=?",(make_password_hash('gpcta-local-test'),fixture.user_id))
            db.execute("UPDATE products SET name='Cerveja sintética 2' WHERE name='Cerveja'")
            db.execute("UPDATE players SET name='Peladeiro sintético GPCTA',war_name='Teste mobile' WHERE id=?",(fixture.player_id,))
            db.execute("UPDATE products SET name='Batata Ruffles sintética',price_cents=1200 WHERE id=?",(fixture.product_id,))
            db.commit()

        print(f'SOMENTE DADOS SINTÉTICOS. Pedidos locais #{first} (R$20) e #{second} (R$44).')
        print('Login sintético: teste / gpcta-local-test')
        print('Banco temporário; nenhum dado real. Abra http://127.0.0.1:5057/login')
        app.run(host='127.0.0.1',port=5057,debug=False,use_reloader=False)
    finally:
        fixture.tearDown()
        fixture.doCleanups()


if __name__=='__main__':
    main()
