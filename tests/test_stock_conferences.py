import re
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from tests import test_mercadopago as fixtures

app = fixtures.app
from src.db import connect_sqlite, get_db
from src.catalog import SPORTS_MATERIAL_CATEGORY
from src.services.stock_conferences import apply_conference, application
from src.services.stock_report_pdf import stock_report_data, build_stock_report_pdf


class StockConferenceTest(unittest.TestCase):
    setUp = fixtures.MercadoPagoFlowTest.setUp
    tearDown = fixtures.MercadoPagoFlowTest.tearDown
    create_sports_schema = fixtures.MercadoPagoFlowTest.create_sports_schema
    conference_snapshot = fixtures.MercadoPagoFlowTest.conference_snapshot

    def login(self):
        with self.client.session_transaction() as s:
            s['user_id'] = self.user_id

    def execute(self, sql, params=()):
        # Each assertion uses a fresh independent connection.
        with sqlite3.connect(app.config['DATABASE']) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, params).fetchall()
        return rows

    def register(self, physical=17, month='2026-08', **extra):
        self.login()
        products = self.execute('SELECT id,stock FROM products WHERE active=1 AND category<>?', (SPORTS_MATERIAL_CATEGORY,))
        data = {'conference_month':month, 'stock_snapshot':self.conference_snapshot()}
        for p in products:
            data[f"physical_{p['id']}"] = str(physical if p['id']==self.product_id else p['stock'])
            data[f"reason_{p['id']}"] = 'Contagem'
        data.update(extra)
        return self.client.post('/stock/conference', data=data)

    def prepare(self):
        self.execute('UPDATE products SET stock=20 WHERE id=?', (self.product_id,))

    def cid(self):
        return self.execute('SELECT MAX(id) FROM stock_conferences')[0][0]

    def apply(self, cid=None):
        return self.client.post(f'/stock/conference/{cid or self.cid()}/apply', follow_redirects=True)

    def test_registration_redirect_status_summary_and_signed_snapshot(self):
        self.prepare()
        response = self.register()
        self.assertIn('conference_id=', response.location)
        self.assertEqual(self.execute('SELECT stock FROM products')[0][0],20)
        page=self.client.get(response.location).get_data(as_text=True)
        for value in ('O estoque ainda não foi alterado', 'Pendente de aplicação', '1 itens a ajustar', 'Negativas: -3', 'Confirmar e aplicar ajuste'):
            self.assertIn(value,page)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_adjustments')[0][0],0)
        self.assertIn('Recarregue',self.register(stock_snapshot='tampered').get_data(as_text=True))

    def test_negative_positive_zero_and_repeated_application_persist(self):
        with patch('src.routes.products.notify_low_stock') as alert:
            for physical, month in ((17,'2026-01'),(23,'2026-02'),(20,'2026-03')):
                self.prepare(); self.register(physical,month)
                cid=self.cid(); page=self.apply(cid).get_data(as_text=True)
                self.assertIn('Ajuste aplicado ao estoque',page)
                self.assertEqual(self.execute('SELECT stock FROM products')[0][0],physical)
                adjustments=self.execute('SELECT * FROM stock_adjustments WHERE conference_id=?',(cid,))
                self.assertEqual(len(adjustments),int(physical!=20))
                if adjustments: self.assertEqual(adjustments[0]['difference'],physical-20)
                self.apply(cid)
                self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications WHERE conference_id=?',(cid,))[0][0],1)
            self.assertEqual(alert.call_count,2)

    def test_simultaneous_applications(self):
        self.prepare();self.register();cid=self.cid();path=app.config['DATABASE'];barrier=Barrier(2)
        def worker():
            db=connect_sqlite(path)
            try:
                barrier.wait()
                try: apply_conference(db,cid,self.user_id);return 'applied'
                except ValueError: return 'blocked'
            finally: db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:worker(),range(2)))
        self.assertCountEqual(results,['applied','blocked'])
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_adjustments')[0][0],1)
        self.assertEqual(self.execute('SELECT stock FROM products')[0][0],17)

    def test_rollback_all_items_and_claim(self):
        self.prepare()
        second=self.execute("INSERT INTO products(name,category,stock,price_cents) VALUES('Second','Bebida',20,100) RETURNING id")[0][0]
        self.register(**{f'physical_{second}':'18'})
        self.execute('UPDATE products SET stock=19 WHERE id=?',(second,))
        with patch('src.routes.products.notify_low_stock') as alert:
            self.assertIn('mudou desde',self.apply().get_data(as_text=True));alert.assert_not_called()
        self.assertEqual(self.execute('SELECT stock FROM products WHERE id=?',(self.product_id,))[0][0],20)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_adjustments')[0][0],0)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications')[0][0],0)

    def test_delete_pending_recreate_and_block_applied_deletion(self):
        self.prepare();self.register();old=self.cid()
        self.client.post(f'/stock/conference/{old}/delete')
        self.register();new=self.cid();self.assertNotEqual(old,new)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_audit WHERE action=\'EXCLUIDA\'')[0][0],1)
        with patch('src.routes.products.notify_low_stock'):self.apply(new)
        page=self.client.post(f'/stock/conference/{new}/delete',follow_redirects=True).get_data(as_text=True)
        self.assertIn('procedimento específico e auditável',page)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conferences')[0][0],1)

    def test_round_trip_movement_and_stale_form(self):
        self.prepare();self.register()
        self.execute('UPDATE products SET stock=stock-1')
        self.execute('UPDATE products SET stock=stock+1')
        self.assertIn('mudou desde',self.apply().get_data(as_text=True))
        snapshot=self.conference_snapshot()
        self.execute('UPDATE products SET stock=19')
        page=self.register(17,'2026-09',stock_snapshot=snapshot).get_data(as_text=True)
        self.assertIn('abertura do formulário',page)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conferences')[0][0],1)

    def test_inactive_missing_empty_and_legacy_items(self):
        for mode in ('inactive','missing','empty','legacy'):
            with self.subTest(mode=mode):
                self.prepare();self.execute('UPDATE products SET active=1');self.register(month=f'2026-0{len(self.execute("SELECT id FROM stock_conferences"))+1}')
                cid=self.cid()
                if mode=='inactive':self.execute('UPDATE products SET active=0')
                elif mode=='missing':self.execute('UPDATE stock_conference_items SET product_id=999 WHERE conference_id=?',(cid,))
                elif mode=='empty':self.execute('DELETE FROM stock_conference_items WHERE conference_id=?',(cid,))
                else:self.execute('UPDATE stock_conference_items SET expected_revision=NULL WHERE conference_id=?',(cid,))
                self.apply(cid)
                self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications')[0][0],0)

    def test_sports_excluded_and_legacy_state_belongs_to_id(self):
        self.prepare()
        self.execute("INSERT INTO products(name,category,stock,price_cents) VALUES('Camisa',?,99,100)",(SPORTS_MATERIAL_CATEGORY,))
        self.register()
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_items')[0][0],1)
        cid=self.cid()
        self.execute("INSERT INTO stock_conference_audit(conference_month,action,details,user_id) VALUES('2026-08','APLICADA','Conferência #999 aplicada ao estoque.',?)",(self.user_id,))
        with app.app_context():self.assertIsNone(application(get_db(),cid))
        self.execute("INSERT INTO stock_conference_audit(conference_month,action,details,user_id) VALUES('2026-08','APLICADA',?,?)",(f'Conferência #{cid} aplicada ao estoque.',self.user_id))
        page=self.client.get(f'/stock/conference?conference_id={cid}').get_data(as_text=True)
        self.assertIn('Ajuste aplicado ao estoque',page)
        self.assertIn('procedimento específico', self.client.post(f'/stock/conference/{cid}/delete',follow_redirects=True).get_data(as_text=True))

    def test_permissions_and_csrf(self):
        self.prepare();self.register();cid=self.cid()
        self.execute("UPDATE users SET role='staff' WHERE id=?",(self.user_id,))
        page=self.client.get(f'/stock/conference?conference_id={cid}').get_data(as_text=True)
        self.assertNotIn('data-apply-stock>Confirmar',page)
        for action in ('apply','delete'):
            self.client.post(f'/stock/conference/{cid}/{action}')
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications')[0][0],0)
        self.execute("UPDATE users SET role='manager' WHERE id=?",(self.user_id,))
        app.config['WTF_CSRF_ENABLED']=True
        self.client.post(f'/stock/conference/{cid}/apply')
        self.client.post(f'/stock/conference/{cid}/delete')
        self.client.post('/stock/conference',data={})
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications')[0][0],0)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conferences')[0][0],1)

    def test_report_adjustments_filters_corrections_and_pdf(self):
        self.prepare();self.register()
        with patch('src.routes.products.notify_low_stock'):self.apply()
        self.execute("INSERT INTO stock_adjustments(product_id,previous_stock,new_stock,difference,reason,created_at) VALUES(?,17,20,3,'Manual','2026-01-01 12:00:00')",(self.product_id,))
        self.execute("INSERT INTO stock_adjustments(product_id,previous_stock,new_stock,difference,reason) VALUES(?,20,22,2,'Correção da reposição #1: teste')",(self.product_id,))
        with app.app_context():
            rows=stock_report_data(get_db());row=rows[0]
            self.assertEqual((row['stock'],row['adjustments_positive'],row['adjustments_negative'],row['net']),(17,3,3,0))
            filtered=stock_report_data(get_db(),'2026-01-01','2026-01-01')[0]
            self.assertEqual((filtered['adjustments_positive'],filtered['adjustments_negative']),(3,0))
            pdf=build_stock_report_pdf(rows,None,None)
            self.assertTrue(pdf.getvalue().startswith(b'%PDF'))

    def test_alert_failure_does_not_undo_application(self):
        self.prepare(); self.register()
        with patch('src.routes.products.notify_low_stock', side_effect=RuntimeError('simulated alert failure')):
            page = self.apply().get_data(as_text=True)
        self.assertIn('O ajuste foi aplicado, mas o alerta', page)
        self.assertEqual(self.execute('SELECT stock FROM products')[0][0], 17)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications')[0][0], 1)

    def test_deleted_or_deactivated_product_since_form(self):
        self.prepare(); self.login()
        for mutation in ('DELETE FROM products', 'UPDATE products SET active=0'):
            snapshot = self.conference_snapshot()
            self.execute(mutation)
            response = self.client.post('/stock/conference', data={
                'conference_month': '2026-08', 'stock_snapshot': snapshot,
                f'physical_{self.product_id}': '17', f'reason_{self.product_id}': 'Count',
            })
            self.assertIn('abertura do formulário', response.get_data(as_text=True))
            self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conferences')[0][0], 0)
            self.execute("INSERT INTO products(id,name,category,stock,price_cents) VALUES(?, 'Replacement','Bar',20,100)", (self.product_id,)) if mutation.startswith('DELETE') else None

    def test_sqlite_schema_upgrade_preserves_legacy_data(self):
        import tempfile
        from pathlib import Path
        from src.db import SCHEMA, initialize_sqlite_database
        old = SCHEMA.replace('    stock_revision INTEGER NOT NULL DEFAULT 0,\n', '')
        old = old.replace('    expected_revision INTEGER,\n', '')
        old = old.replace('    conference_id INTEGER REFERENCES stock_conferences(id),\n', '')
        old = re.sub(r'CREATE TABLE IF NOT EXISTS stock_conference_applications \(.*?\);', '', old, flags=re.S)
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'legacy.db')
            with sqlite3.connect(path) as db:
                db.executescript(old)
                db.execute("INSERT INTO products(name,category,stock,price_cents) VALUES('Legacy','Bar',20,100)")
                db.execute("INSERT INTO stock_conferences(conference_month) VALUES('2025-01')")
                db.execute("INSERT INTO stock_conference_items(conference_id,product_id,expected_stock,physical_stock,difference) VALUES(1,1,20,17,-3)")
            initialize_sqlite_database(path)
            initialize_sqlite_database(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT stock,stock_revision FROM products').fetchone(), (20,0))
                self.assertIsNone(db.execute('SELECT expected_revision FROM stock_conference_items').fetchone()[0])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM stock_conference_applications').fetchone()[0],0)
                db.execute('UPDATE products SET stock=19')
                db.execute('UPDATE products SET stock=20')
                self.assertEqual(db.execute('SELECT stock_revision FROM products').fetchone()[0],2)

    def test_database_error_rolls_back_stock_history_and_application(self):
        from src.db import DbWrapper
        self.prepare(); self.register()
        original = DbWrapper.execute

        def fail_adjustment(db, sql, params=None):
            if 'INSERT INTO stock_adjustments' in sql:
                raise sqlite3.IntegrityError('simulated adjustment failure')
            return original(db, sql, params)

        with patch.object(DbWrapper, 'execute', fail_adjustment), patch('src.routes.products.notify_low_stock') as alert:
            self.apply()
            alert.assert_not_called()
        self.assertEqual(self.execute('SELECT stock FROM products')[0][0], 20)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_adjustments')[0][0], 0)
        self.assertEqual(self.execute('SELECT COUNT(*) FROM stock_conference_applications')[0][0], 0)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM stock_conference_audit WHERE action='APLICADA'")[0][0], 0)
