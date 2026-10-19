"""Stage 1 contract; never connects to an environment URL or production."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
import os
from pathlib import Path
from threading import Barrier
import tempfile
import unittest
from unittest.mock import patch
import uuid

from src.db import DbWrapper, connect_sqlite, initialize_sqlite_database
from src.football_benefit_schema import schema_sql
from src.services.football_beneficiaries import list_memberships, save_membership, MembershipConflict

MIGRATION = Path('supabase/migrations/20261018070000_football_benefits.sql')


class MembershipContract:
    def prepare(self):
        self.db = self.connect()
        self.addCleanup(self.db.close)
        self.actor = self.db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('benefit.manager','Manager','x','manager')").lastrowid
        self.operator = self.db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('benefit.operator','Operator','x','football_manager')").lastrowid
        self.client_user = self.db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('benefit.client','Client','x','client')").lastrowid
        self.player = self.db.execute("INSERT INTO players(name) VALUES('Beneficiary')").lastrowid
        self.db.commit()

    def save(self, **values):
        data = dict(player_id=self.player, category='DIRETORIA', starts_on='2026-01-01', reason='Cadastro aprovado')
        data.update(values)
        return save_membership(self.db, self.actor, **data)

    def count(self, table):
        return self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_categories_do_not_grant_roles(self):
        self.save(ends_on='2026-06-30')
        self.save(category='OFICIAL_GENERAL', starts_on='2026-07-01')
        self.assertEqual(self.count('football_directorships'), 2)
        self.assertEqual(self.count('football_benefit_events'), 2)
        self.assertEqual(self.db.execute('SELECT role FROM users WHERE id=?', (self.client_user,)).fetchone()[0], 'client')
        self.assertEqual(self.count('users'), 3)
        self.assertEqual(self.count('football_benefit_sessions'), 0)
        self.assertEqual(self.count('football_presence_events'), 0)
        self.assertEqual(self.count('football_benefit_allocations'), 0)

    def test_operator_allowed_but_client_and_inactive_denied(self):
        save_membership(self.db, self.operator, player_id=self.player, category='OFICIAL_GENERAL', starts_on='2026-01-01', reason='Cadastro autorizado')
        for uid in (self.client_user, 99999):
            with self.assertRaises(PermissionError):
                save_membership(self.db, uid)
        self.db.execute('UPDATE users SET active=0 WHERE id=?', (self.actor,)); self.db.commit()
        with self.assertRaises(PermissionError):
            self.save()
        self.assertEqual(self.count('football_benefit_events'), 1)

    def test_invalid_input_does_not_persist(self):
        for values in (dict(player_id=999999), dict(category='GENERAL'), dict(starts_on='2026-02-30'),
                       dict(starts_on='2026-1-01'), dict(ends_on='2025-12-31'), dict(reason=' '),
                       dict(reason='x'*1001)):
            with self.assertRaises(ValueError):
                self.save(**values)
        self.assertEqual(self.count('football_directorships'), 0)
        self.assertEqual(self.count('football_benefit_events'), 0)

    def test_duplicate_cross_category_overlap_including_endpoints(self):
        self.save(ends_on='2026-06-30')
        for values in (dict(), dict(category='OFICIAL_GENERAL'), dict(starts_on='2026-06-30'),
                       dict(starts_on='2025-01-01', ends_on='2026-01-01')):
            with self.assertRaises(MembershipConflict):
                self.save(**values)
        self.assertEqual(self.count('football_directorships'), 1)

    def test_end_amend_revisions_and_audit_persist_new_connection(self):
        mid = self.save()
        self.save(membership_id=mid, revision=1, category='OFICIAL_GENERAL', reason='Correção de categoria')
        with self.assertRaises(MembershipConflict):
            self.save(membership_id=mid, revision=1)
        self.save(membership_id=mid, revision=2, end_only=True, ends_on='2026-06-30', reason='Fim de mandato')
        self.save(starts_on='2026-07-01')
        other = self.connect()
        try:
            events = other.execute('SELECT * FROM football_benefit_events ORDER BY id').fetchall()
            self.assertEqual([e['event_type'] for e in events], ['REGISTERED', 'AMENDED', 'ENDED', 'REGISTERED'])
            self.assertEqual(json.loads(events[1]['before_snapshot'])['category'], 'DIRETORIA')
            self.assertEqual(json.loads(events[2]['after_snapshot'])['ends_on'], '2026-06-30')
            self.assertEqual(other.execute('SELECT revision FROM football_directorships WHERE id=?', (mid,)).fetchone()[0], 3)
        finally:
            other.close()

    def test_status_derived_from_dates_inclusive(self):
        self.save(starts_on='2025-01-01', ends_on='2025-12-31')
        self.save(ends_on='2026-10-18')
        self.save(starts_on='2026-10-19')
        with patch('src.services.football_beneficiaries.local_today', return_value=date(2026,10,18)):
            self.assertEqual([r['validity_status'] for r in list_memberships(self.db)], ['Agendado','Ativo','Encerrado'])

    def test_audit_failure_rolls_back_membership(self):
        execute = self.db.execute
        def fail(sql, params=None):
            if 'INSERT INTO football_benefit_events' in sql:
                raise RuntimeError('Simulated audit failure')
            return execute(sql, params)
        with patch.object(self.db, 'execute', side_effect=fail), self.assertRaises(RuntimeError):
            self.save()
        self.assertEqual(self.count('football_directorships'), 0)

    def test_audit_immutable_and_membership_cannot_be_deleted(self):
        mid = self.save()
        for sql in ('DELETE FROM football_benefit_events', "UPDATE football_benefit_events SET reason='changed'",
                    'DELETE FROM football_directorships', 'DELETE FROM players'):
            with self.assertRaises(Exception):
                self.db.execute(sql)
            self.db.rollback()
        self.assertEqual(self.count('football_benefit_events'), 1)
        self.assertEqual(self.count('football_directorships'), 1)

    def test_database_rejects_overlap_and_unversioned_edits(self):
        mid = self.save(ends_on='2026-06-30')
        for sql, args in [
            ('INSERT INTO football_directorships(player_id,category,starts_on,ends_on,created_by,updated_by,change_reason) VALUES(?,?,?,?,?,?,?)',
             (self.player,'OFICIAL_GENERAL','2026-01-01','2026-03-01',self.actor,self.actor,'Conflicting period')),
            ('UPDATE football_directorships SET category=? WHERE id=?', ('OFICIAL_GENERAL',mid))]:
            with self.assertRaises(Exception):
                self.db.execute(sql,args)
            self.db.rollback()
        self.assertEqual(self.count('football_directorships'), 1)

    def test_first_match_and_allocation_constraints(self):
        mid = self.save()
        s = self.db.execute("INSERT INTO football_sumulas(match_date,day_pelada) VALUES('2026-10-21','QUARTA')").lastrowid
        m1 = self.db.execute('INSERT INTO football_matches(sumula_id,number) VALUES(?,1)', (s,)).lastrowid
        m2 = self.db.execute('INSERT INTO football_matches(sumula_id,number) VALUES(?,2)', (s,)).lastrowid
        self.db.commit()
        with self.assertRaises(Exception):
            self.db.execute('INSERT INTO football_benefit_sessions(sumula_id,first_match_id,created_by) VALUES(?,?,?)', (s,m2,self.actor))
        self.db.rollback()
        sid = self.db.execute('INSERT INTO football_benefit_sessions(sumula_id,first_match_id,created_by) VALUES(?,?,?)', (s,m1,self.actor)).lastrowid
        self.db.execute("INSERT INTO football_benefit_allocations(session_id,player_id,directorship_id,position,status) VALUES(?,?,?,'DEFESA','RESERVED')", (sid,self.player,mid));self.db.commit()
        for position in ('DEFESA','MEIO','GOL'):
            with self.assertRaises(Exception):
                self.db.execute("INSERT INTO football_benefit_allocations(session_id,player_id,directorship_id,position,status) VALUES(?,?,?,?,'RESERVED')", (sid,self.player,mid,position))
            self.db.rollback()
        with self.assertRaises(Exception):
            self.db.execute('UPDATE football_matches SET number=3 WHERE id=?',(m1,))
        self.db.rollback()
        with self.assertRaises(Exception):
            self.db.execute('DELETE FROM football_sumulas WHERE id=?',(s,))
        self.db.rollback()

    def test_database_dates_and_presence_immutability(self):
        for start in ('2026-02-30', '2026-13-01', 'invalid'):
            with self.assertRaises(Exception):
                self.db.execute("""INSERT INTO football_directorships
                    (player_id,category,starts_on,created_by,updated_by,change_reason)
                    VALUES(?,'DIRETORIA',?,?,?,'Data inválida')""", (self.player,start,self.actor,self.actor))
            self.db.rollback()
        s = self.db.execute("INSERT INTO football_sumulas(match_date,day_pelada) VALUES('2026-10-21','QUARTA')").lastrowid
        self.db.execute("""INSERT INTO football_presence_events(sumula_id,player_id,event_type,source,
            source_event_id,occurred_at,reason) VALUES(?,?,'NETWORK_OBSERVED','AGENT','probe-1','2026-10-21 10:00:00','Observed only')""",(s,self.player))
        self.db.commit()
        for sql in ('DELETE FROM football_presence_events', "UPDATE football_presence_events SET event_type='CONFIRMED'"):
            with self.assertRaises(Exception):
                self.db.execute(sql)
            self.db.rollback()
        with self.assertRaises(Exception):
            self.db.execute("""INSERT INTO football_presence_events(sumula_id,player_id,event_type,source,
                source_event_id,occurred_at,reason) VALUES(?,?,'CONFIRMED','AGENT','probe-2','2026-10-21','Must not confirm')""", (s,self.player))
        self.db.rollback()

    def test_concurrent_creation_has_one_membership_and_one_event(self):
        self.db.commit()
        def create():
            db = self.connect()
            try:
                save_membership(db,self.actor,player_id=self.player,category='DIRETORIA',starts_on='2026-01-01',reason='Concurrent registration')
                return 'created'
            except MembershipConflict:
                return 'conflict'
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertCountEqual(results,['created','conflict'])
        self.assertEqual(self.count('football_benefit_events'),1)


class SQLiteMembershipTest(MembershipContract, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'benefit.db')
        initialize_sqlite_database(self.path)
        self.prepare()

    def connect(self):
        return connect_sqlite(self.path)

    def test_schema_reapplication_is_non_destructive(self):
        self.save()
        self.db.conn.executescript(schema_sql())
        self.db.conn.executescript(schema_sql())
        self.assertEqual(self.count('football_benefit_events'),1)


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'requires disposable local PostgreSQL')
class PostgresMembershipTest(MembershipContract, unittest.TestCase):
    def setUp(self):
        import psycopg2
        from psycopg2.extras import DictCursor
        socket = os.environ['GPCTA_TEST_POSTGRES_SOCKET']
        if not socket.startswith('/tmp/gpcta-pg.'):
            raise RuntimeError('Only disposable local PostgreSQL sockets are allowed')
        self.args = dict(host=socket,port=55439,dbname='postgres',cursor_factory=DictCursor)
        self.schema = 'benefit_'+uuid.uuid4().hex
        self.admin = psycopg2.connect(**self.args); self.admin.autocommit=True
        with self.admin.cursor() as cur:
            cur.execute('CREATE SCHEMA '+self.schema)
        self.addCleanup(self.cleanup)
        db = self.connect()
        try:
            # Actual repository parent definitions, converted as in init_postgres.
            from src.db import SCHEMA
            with db.conn.cursor() as cur:
                for table in ('users','players','football_sumulas','football_matches'):
                    definition = SCHEMA.split('CREATE TABLE IF NOT EXISTS '+table+' (',1)[1].split('\n);',1)[0]
                    cur.execute(('CREATE TABLE '+table+' ('+definition+'\n);').replace('INTEGER PRIMARY KEY AUTOINCREMENT','SERIAL PRIMARY KEY').replace(' COLLATE NOCASE',''))
                cur.execute(MIGRATION.read_text())
                cur.execute(MIGRATION.read_text())
            db.commit()
        finally:
            db.close()
        self.prepare()

    def connect(self):
        import psycopg2
        conn=psycopg2.connect(**self.args)
        with conn.cursor() as cur:
            cur.execute('SET search_path TO '+self.schema)
        conn.commit()
        return DbWrapper(conn,True)

    def cleanup(self):
        with self.admin.cursor() as cur:
            cur.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.admin.close()

    def test_raw_concurrent_bounded_periods_cannot_overlap(self):
        self.db.commit()
        barrier = Barrier(2)
        def insert():
            db = self.connect()
            try:
                barrier.wait(timeout=5)
                db.execute("""INSERT INTO football_directorships
                    (player_id,category,starts_on,ends_on,created_by,updated_by,change_reason)
                    VALUES(?,'DIRETORIA','2026-01-01','2026-06-30',?,?,'Raw concurrent test')""",
                    (self.player,self.actor,self.actor))
                db.commit()
                return True
            except Exception:
                db.rollback()
                return False
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: insert(), range(2)))
        self.assertCountEqual(results,[True,False])
        self.assertEqual(self.count('football_directorships'),1)

    def test_rls_blocks_unprivileged_even_with_table_grants(self):
        self.save()
        self.db.commit()
        another = self.db.execute("INSERT INTO players(name) VALUES('RLS candidate')").lastrowid
        self.db.commit()
        role = 'benefit_reader_'+uuid.uuid4().hex
        with self.admin.cursor() as cur:
            cur.execute('CREATE ROLE '+role+' NOLOGIN NOSUPERUSER NOBYPASSRLS')
            cur.execute('GRANT USAGE ON SCHEMA '+self.schema+' TO '+role)
            cur.execute('GRANT SELECT,INSERT ON ALL TABLES IN SCHEMA '+self.schema+' TO '+role)
            cur.execute('GRANT USAGE ON ALL SEQUENCES IN SCHEMA '+self.schema+' TO '+role)
            cur.execute('GRANT UPDATE ON '+self.schema+'.players TO '+role)
        try:
            with self.db.conn.cursor() as cur:
                cur.execute('SET LOCAL ROLE '+role)
                cur.execute('SELECT COUNT(*) FROM football_directorships')
                self.assertEqual(cur.fetchone()[0],0)
                with self.assertRaises(Exception) as denied:
                    cur.execute("""INSERT INTO football_directorships
                        (player_id,category,starts_on,created_by,updated_by,change_reason)
                        VALUES(%s,'DIRETORIA','2027-01-01',%s,%s,'RLS must deny')""",
                        (another,self.actor,self.actor))
                self.assertEqual(denied.exception.pgcode, '42501')
                self.assertIn('row-level security', str(denied.exception))
            self.db.rollback()
        finally:
            self.db.rollback()
            with self.admin.cursor() as cur:
                cur.execute('DROP OWNED BY '+role)
                cur.execute('DROP ROLE '+role)

    def test_migration_matches_shared_definition(self):
        self.assertTrue(MIGRATION.read_text().endswith(schema_sql(True)))
        rows=self.db.execute("SELECT relname,relrowsecurity FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relname IN ('football_directorships','football_presence_events','football_benefit_sessions','football_benefit_allocations','football_benefit_events')").fetchall()
        self.assertEqual(len(rows),5)
        self.assertTrue(all(r['relrowsecurity'] for r in rows))


# Shared page fixture uses only a temporary SQLite database.
import test_purchase_planning as pages


class BeneficiaryPagesTest(unittest.TestCase):
    setUp = pages.PurchasePlanningPageTest.setUp
    login = pages.PurchasePlanningPageTest.login
    path = '/futebol/gozada/beneficiarios'

    def db(self):
        return connect_sqlite(pages.app.config['DATABASE'])

    def post(self, **values):
        data=dict(player_id=self.player,category='DIRETORIA',starts_on='2026-01-01',reason='Cadastro de diretoria')
        data.update(values)
        return self.client.post(self.path,data=data)

    def prepare_player(self):
        db=self.db()
        self.player=db.execute("INSERT INTO players(name) VALUES('Diretor <Teste>')").lastrowid
        self.operator=db.execute("INSERT INTO users(username,name,password_hash,role) VALUES('operator','Operador','x','football_manager')").lastrowid
        db.commit();db.close()

    def test_access_server_side_and_no_role_escalation(self):
        self.prepare_player()
        self.assertEqual(self.client.get(self.path,headers={'Accept':'application/json'}).status_code,401)
        for uid in (self.staff,self.client_user):
            self.login(uid)
            self.assertEqual(self.client.get(self.path,headers={'Accept':'application/json'}).status_code,403)
            for action in ('create', 'edit', 'end'):
                self.assertEqual(self.client.post(self.path,data={'action':action,'membership_id':1,'revision':1},headers={'Accept':'application/json'}).status_code,403)
        for uid in (self.manager,self.operator):
            self.login(uid)
            response=self.client.get(self.path)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn('class="active" href="'+self.path+'"',response.get_data(as_text=True))
        self.login(self.operator)
        self.assertEqual(self.post().status_code,303)
        self.assertEqual(self.client.get('/stock',headers={'Accept':'application/json'}).status_code,403)

    def test_create_edit_end_history_and_escape(self):
        self.prepare_player();self.login(self.manager)
        self.assertEqual(self.post().status_code,303)
        page=self.client.get(self.path).get_data(as_text=True)
        self.assertIn('Diretor &lt;Teste&gt;',page)
        self.assertIn('Histórico de alterações',page)
        db=self.db();mid=db.execute('SELECT id FROM football_directorships').fetchone()[0];db.close()
        self.assertEqual(self.post(action='edit',membership_id=mid,revision=1,category='OFICIAL_GENERAL').status_code,303)
        self.assertEqual(self.post(action='end',membership_id=mid,revision=2,ends_on='2026-06-30').status_code,303)
        self.assertEqual(self.post(action='edit',membership_id=mid,revision=1).status_code,409)
        self.assertEqual(self.post(player_id='999999').status_code,400)

    def test_title_contains_no_history_navigation(self):
        import re
        self.login(self.manager)
        page = self.client.get(self.path+'?history_page=2').get_data(as_text=True)
        title = re.search(r'<title>(.*?)</title>', page, re.S).group(1)
        self.assertIn('Beneficiários da Gozada Inteligente', title)
        self.assertNotIn('<nav', title)
        self.assertEqual(page.count('aria-label="Páginas do histórico"'), 1)

    def test_valid_csrf_and_beneficiary_account_keeps_client_role(self):
        import re
        self.prepare_player();self.login(self.manager)
        db=self.db()
        db.execute('UPDATE users SET player_id=? WHERE id=?',(self.player,self.client_user))
        db.commit();db.close()
        pages.app.config['WTF_CSRF_ENABLED']=True
        page=self.client.get(self.path).get_data(as_text=True)
        token=re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
        self.assertEqual(self.post(csrf_token=token).status_code,303)
        db=self.db()
        self.assertEqual(db.execute('SELECT role FROM users WHERE id=?',(self.client_user,)).fetchone()[0],'client')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM football_benefit_events').fetchone()[0],1)
        db.close()

    def test_csrf_and_inactive_user_cannot_mutate(self):
        self.prepare_player();self.login(self.manager)
        pages.app.config['WTF_CSRF_ENABLED']=True
        self.assertEqual(self.post().status_code,303)  # project CSRF handler redirects
        db=self.db();self.assertEqual(db.execute('SELECT COUNT(*) FROM football_directorships').fetchone()[0],0)
        db.execute('UPDATE users SET active=0 WHERE id=?',(self.manager,));db.commit();db.close()
        pages.app.config['WTF_CSRF_ENABLED']=False
        self.assertNotEqual(self.post().status_code,200)
        db=self.db();self.assertEqual(db.execute('SELECT COUNT(*) FROM football_directorships').fetchone()[0],0);db.close()
