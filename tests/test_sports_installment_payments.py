import sqlite3
import hashlib
import hmac
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import patch

import test_sports_installments as fixtures
from src.services.mercadopago import MercadoPagoError
from src.services.sports_installments import create_installment_plan
from src.services.sports_installment_payments import create_installment_payment_attempt


class InstallmentPaymentsTest(unittest.TestCase):
    setUp = fixtures.SportsInstallmentsTest.setUp
    sale = fixtures.SportsInstallmentsTest.sale

    def setup_plan(self):
        sale_id=self.sale()
        player_id=self.db.execute("INSERT INTO players(name,email) VALUES('Cliente','client@example.com')").lastrowid
        self.db.execute('UPDATE sales SET player_id=? WHERE id=?',(player_id,sale_id))
        self.db.commit()
        result=create_installment_plan(self.db,sale_id,10000,self.base_date)
        self.sale_id=sale_id
        self.player_id=player_id
        self.installments=result['installments']
        return result

    def order(self, suffix='1'):
        return {'id':f'ORD-{suffix}','transactions':{'payments':[{'id':f'PAY-{suffix}','payment_method':{'qr_code':f'QR-{suffix}','qr_code_base64':'encoded','ticket_url':'https://example.invalid/pix'}}]}}

    def charge(self, number=1, key=None):
        return create_installment_payment_attempt(self.db,self.installments[number-1]['id'],'fake-token',key)

    def snapshot(self):
        return {table:[tuple(r) for r in self.db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()]
                for table in ('sales','sale_items','sports_installment_plans','sports_installments','products','sports_product_variants','sports_sale_item_details')}

    def test_first_installment_amount_from_database(self):
        self.setup_plan()
        before=self.snapshot()
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order()) as create:
            attempt=self.charge()
        self.assertEqual(create.call_args.args[2],3333)
        self.assertEqual(attempt['amount_cents'],3333)
        self.assertEqual(attempt['status'],'pending')
        self.assertEqual(before,self.snapshot())

    def test_2_and_3_can_be_paid_early(self):
        self.setup_plan()
        for number,amount in ((2,3333),(3,3334)):
            with self.subTest(number=number), patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order(str(number))) as create:
                attempt=self.charge(number)
                self.assertEqual(create.call_args.args[2],amount)
                self.assertEqual(attempt['status'],'pending')

    def test_paid_installment_blocks_api(self):
        self.setup_plan()
        self.db.execute("UPDATE sports_installments SET status='paid' WHERE id=?",(self.installments[0]['id'],))
        self.db.commit()
        with patch('src.services.sports_installment_payments.create_pix_order') as create,self.assertRaises(ValueError):
            self.charge()
        create.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sports_installment_payment_attempts').fetchone()[0],0)

    def test_expired_qr_new_attempt_retains_history_and_due_date(self):
        self.setup_plan()
        before=self.snapshot()
        with patch('src.services.sports_installment_payments.create_pix_order',side_effect=[self.order('1'),self.order('2')]):
            first=self.charge()
            expired=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
            self.db.execute('UPDATE sports_installment_payment_attempts SET expires_at=? WHERE id=?',(expired,first['id']))
            self.db.commit()
            second=self.charge()
        rows=self.db.execute('SELECT * FROM sports_installment_payment_attempts ORDER BY id').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['status'],'expired')
        self.assertEqual(rows[0]['qr_code'],'QR-1')
        self.assertEqual(second['qr_code'],'QR-2')
        self.assertNotEqual(first['external_reference'],second['external_reference'])
        self.assertNotEqual(first['idempotency_key'],second['idempotency_key'])
        self.assertEqual(before,self.snapshot())

    def test_same_key_creates_once(self):
        self.setup_plan()
        key=str(uuid4())
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order()) as create:
            first=self.charge(key=key)
            second=self.charge(key=key)
        self.assertEqual(first,second)
        self.assertEqual(create.call_count,1)
        self.assertEqual(create.call_args.args[3],key)
        self.assertIn(f"p{self.installments[0]['plan_id']}_i1_a",first['external_reference'])

    def test_different_key_reuses_live_attempt(self):
        self.setup_plan()
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order()) as create:
            first=self.charge()
            second=self.charge()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(create.call_count,1)

    def test_key_cannot_be_reused_for_another_installment(self):
        self.setup_plan()
        key=str(uuid4())
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order()):
            self.charge(key=key)
        with patch('src.services.sports_installment_payments.create_pix_order') as create,self.assertRaises(ValueError):
            self.charge(2,key)
        create.assert_not_called()

    def test_failure_durable_attempt_without_payment_mutation(self):
        self.setup_plan()
        before=self.snapshot()
        key=str(uuid4())
        with patch('src.services.sports_installment_payments.create_pix_order',side_effect=MercadoPagoError('mock failure')) as create:
            with self.assertRaises(MercadoPagoError):
                self.charge(key=key)
            replay=self.charge(key=key)
        self.assertEqual(create.call_count,1)
        self.assertEqual(replay['status'],'failed')
        self.assertTrue(replay['external_reference'])
        self.assertEqual(before,self.snapshot())

    def test_timeout_preserves_inflight_attempt_and_blocks_new_charge(self):
        self.setup_plan()
        error=MercadoPagoError('mock timeout')
        error.__cause__=TimeoutError('uncertain remote creation')
        before=self.snapshot()
        with patch('src.services.sports_installment_payments.create_pix_order',side_effect=error) as create:
            with self.assertRaises(MercadoPagoError):
                self.charge()
            retry=self.charge()
        self.assertEqual(create.call_count,1)
        self.assertEqual(retry['status'],'creating')
        self.assertEqual(self.snapshot(),before)

    def test_constraints_unique_reference_and_key(self):
        self.setup_plan()
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order()):
            attempt=self.charge()
        for ref,key in ((attempt['external_reference'],str(uuid4())),('unique-ref',attempt['idempotency_key'])):
            with self.subTest(ref=ref),self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('INSERT INTO sports_installment_payment_attempts(installment_id,amount_cents,external_reference,idempotency_key) VALUES(?,?,?,?)',(attempt['installment_id'],3333,ref,key))
            self.db.rollback()

    def test_client_cannot_charge_another_player(self):
        self.setup_plan()
        with patch('src.services.sports_installment_payments.create_pix_order') as create,self.assertRaises(ValueError):
            create_installment_payment_attempt(self.db,self.installments[0]['id'],'fake',player_id=self.player_id+1)
        create.assert_not_called()

    def test_reentrant_retry_while_creating_never_calls_api_twice(self):
        self.setup_plan()
        key=str(uuid4())
        observed=[]
        def provider(*args):
            observed.append(self.charge(key=key))
            return self.order()
        with patch('src.services.sports_installment_payments.create_pix_order',side_effect=provider) as create:
            attempt=self.charge(key=key)
        self.assertEqual(create.call_count,1)
        self.assertEqual(observed[0]['status'],'creating')
        self.assertEqual(observed[0]['id'],attempt['id'])

    def test_missing_qr_keeps_external_order_trace(self):
        self.setup_plan()
        with patch('src.services.sports_installment_payments.create_pix_order',return_value={'id':'ORD-MISSING-QR'}),self.assertRaises(MercadoPagoError):
            self.charge()
        row=self.db.execute('SELECT * FROM sports_installment_payment_attempts').fetchone()
        self.assertEqual((row['status'],row['mercado_pago_order_id']),('failed','ORD-MISSING-QR'))

    def test_initial_provider_approval_does_not_mark_installment_paid(self):
        self.setup_plan()
        before=self.snapshot()
        order=self.order()
        order.update(status='processed',status_detail='accredited',total_paid_amount='33.33')
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=order):
            self.charge()
        self.assertEqual(self.snapshot(),before)

    def test_endpoint_ignores_request_amount(self):
        from app import app
        self.setup_plan()
        saved=dict(app.config)
        self.addCleanup(lambda:app.config.update(saved))
        app.config.update(TESTING=True,WTF_CSRF_ENABLED=False,DATABASE=self.db.conn.execute('PRAGMA database_list').fetchone()[2],DATABASE_URL=None,APP_ENV='local',EXTERNAL_PAYMENTS_ENABLED=True,MERCADOPAGO_ACCESS_TOKEN='fake')
        user_id=self.db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('installment-admin','Admin','hash','manager')").lastrowid
        self.db.commit()
        client=app.test_client()
        with client.session_transaction() as session:
            session['user_id']=user_id
        with patch('src.services.sports_installment_payments.create_pix_order',return_value=self.order()) as create:
            response=client.post(f"/material-esportivo/parcelas/{self.installments[0]['id']}/pix",json={'amount_cents':1,'total_cents':1,'idempotency_key':str(uuid4())})
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(create.call_args.args[2],3333)
        self.assertEqual(response.get_json()['attempt']['amount_cents'],3333)
        # The legacy webhook must not reconcile this independent order as a sale.
        attempt=response.get_json()['attempt']
        before=self.snapshot()
        app.config['MERCADOPAGO_WEBHOOK_SECRET']='fake-webhook-secret'
        stamp='12345'
        request_id='installment-notification'
        signed=f"id:{attempt['mercado_pago_order_id'].lower()};request-id:{request_id};ts:{stamp};"
        digest=hmac.new(b'fake-webhook-secret',signed.encode(),hashlib.sha256).hexdigest()
        with patch('src.routes.sales.apply_mercadopago_status') as reconcile, patch('src.routes.sales.get_order', return_value={'id':attempt['mercado_pago_order_id'], 'external_reference':attempt['external_reference'], 'status':'pending'}):
            webhook=client.post('/webhooks/mercadopago',json={'data':{'id':attempt['mercado_pago_order_id'],'type':'online','external_reference':attempt['external_reference'],'status':'processed','status_detail':'accredited','total_paid_amount':'33.33'}},headers={'X-Request-Id':request_id,'X-Signature':f'ts={stamp},v1={digest}'})
        self.assertEqual(webhook.status_code,200)
        reconcile.assert_not_called()
        self.assertEqual(self.snapshot(),before)



if __name__=='__main__':
    unittest.main()
