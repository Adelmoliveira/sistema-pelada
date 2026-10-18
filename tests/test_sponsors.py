from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
from werkzeug.datastructures import FileStorage

import test_purchase_planning as pages
from src.db import connect_sqlite, DbWrapper
from src.sponsor_schema import schema_sql
from src.services.sponsors import invalidate_cache, process_logo, public_sponsors, validate_link


def png():
    stream = BytesIO()
    Image.new('RGBA', (300, 100), (0, 70, 180, 150)).save(stream, format='PNG')
    return stream.getvalue()


class SponsorPagesTest(unittest.TestCase):
    setUp = pages.PurchasePlanningPageTest.setUp
    login = pages.PurchasePlanningPageTest.login

    def db(self):
        return connect_sqlite(pages.app.config['DATABASE'])

    def revision(self):
        db = self.db()
        try:
            return db.execute('SELECT revision FROM sponsor_catalog').fetchone()[0]
        finally:
            db.close()

    def save(self, **values):
        data = {'name': 'Patrocinador de teste', 'revision': self.revision(), 'active': '1'}
        data.update(values)
        return self.client.post('/admin/sponsors', data=data)

    def add_logo(self, **values):
        values['logo'] = (BytesIO(png()), 'logo.png')
        return self.save(**values)

    def latest(self):
        db = self.db()
        try:
            return dict(db.execute('SELECT * FROM sponsors ORDER BY id DESC LIMIT 1').fetchone())
        finally:
            db.close()

    def test_only_manager_can_change_and_see_admin(self):
        for uid in (self.staff, self.client_user):
            self.login(uid)
            for path in ('/admin/sponsors', '/admin/sponsors/order'):
                response = self.client.post(path, json={'revision': self.revision(), 'ids': []}, headers={'Accept': 'application/json'})
                self.assertEqual(response.status_code, 403)
            self.assertEqual(self.client.get('/admin/sponsors', headers={'Accept': 'application/json'}).status_code, 403)
        self.login(self.manager)
        self.assertEqual(self.client.get('/admin/sponsors').status_code, 200)
        self.assertIn(b'no-store', self.client.get('/admin/sponsors').headers['Cache-Control'].encode())

    def test_anonymous_and_inactive_cannot_edit(self):
        self.assertEqual(self.client.post('/admin/sponsors/order', json={'ids': []}, headers={'Accept': 'application/json'}).status_code, 401)
        self.login(self.manager)
        db = self.db()
        with db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (self.manager,))
        db.close()
        self.assertNotEqual(self.save().status_code, 200)
        self.assertEqual(self.revision(), 1)

    def test_csrf_for_upload_and_reorder(self):
        self.login(self.manager)
        pages.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.save().status_code, 303)
        self.assertEqual(self.client.post('/admin/sponsors/order', json={'revision': 1, 'ids': []}).status_code, 303)
        self.assertEqual(self.revision(), 1)

    def test_empty_catalog_and_idempotent_schema(self):
        self.login(self.manager)
        db = self.db()
        db.conn.executescript(schema_sql())
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors').fetchone()[0], 2)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors WHERE logo_data IS NOT NULL OR active=1').fetchone()[0], 0)
        db.close()
        html = self.client.get('/admin/sponsors').get_data(as_text=True)
        self.assertNotIn('class="sponsor-logos"', html)
        self.assertIn('Aguardando upload', html)

    def test_public_image_and_footer_only_logos(self):
        self.login(self.manager)
        self.assertEqual(self.add_logo(name='Empresa de teste', link_url='https://example.com').status_code, 302)
        row = self.latest()
        self.client.post('/logout')
        url = '/sponsors/{}/logo/{}'.format(row['id'], row['logo_version'])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/webp')
        self.assertIn('public', response.headers['Cache-Control'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.client.get(url, headers={'If-None-Match': response.headers['ETag']}).status_code, 304)
        html = self.client.get('/login').get_data(as_text=True)
        footer = html.split('<footer class="gpcta-footer">')[1].split('</footer>')[0]
        self.assertIn('alt="Empresa de teste"', footer)
        self.assertNotIn('>Empresa de teste<', footer)
        self.assertIn('noopener noreferrer sponsored', footer)
        self.assertLess(footer.index('GPCTA'), footer.index('<img'))

    def test_deactivate_remove_and_old_version(self):
        self.login(self.manager)
        self.add_logo()
        row = self.latest()
        url = '/sponsors/{}/logo/{}'.format(row['id'], row['logo_version'])
        self.save(sponsor_id=row['id'], active='0')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.save(sponsor_id=row['id'], remove_logo='1')
        self.assertIsNone(self.latest()['logo_data'])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get('/sponsors/1/logo/not-a-hash').status_code, 404)

    def test_fixed_slots_and_reorder_persist(self):
        self.login(self.manager)
        self.save(name='B'); first = self.latest()['id']
        self.save(name='A'); second = self.latest()['id']
        revision = self.revision()
        response = self.client.post('/admin/sponsors/order', json={'revision': revision, 'ids': [second, first]})
        self.assertEqual(response.status_code, 200)
        db = self.db()
        rows = db.execute('SELECT name,primary_slot FROM sponsors ORDER BY primary_slot IS NULL,primary_slot,sort_order').fetchall()
        db.close()
        self.assertEqual([row['name'] for row in rows], ['VINAC', 'Prudention', 'A', 'B'])
        self.assertEqual(self.client.post('/admin/sponsors/order', json={'revision': revision, 'ids': [first, second]}).status_code, 409)
        for ids in ([1, first, second], [first], [first, first], [True, second], ['3', second]):
            self.assertEqual(self.client.post('/admin/sponsors/order', json={'revision': self.revision(), 'ids': ids}).status_code, 400)

    def test_primary_can_edit_but_not_move(self):
        self.login(self.manager)
        self.add_logo(sponsor_id=1, name='VINAC', primary_slot='2', sort_order='99')
        db = self.db()
        row = db.execute('SELECT primary_slot,sort_order,logo_data FROM sponsors WHERE id=1').fetchone()
        db.close()
        self.assertEqual((row['primary_slot'], row['sort_order']), (1, 0))
        self.assertTrue(row['logo_data'])
        html = self.client.get('/admin/sponsors').get_data(as_text=True)
        self.assertIn('data-sponsor-slot="1"', html)
        self.assertIn('data-sponsor-slot="2"', html)

    def test_stale_edit_rolls_back(self):
        self.login(self.manager)
        revision = self.revision()
        self.save(name='Primeiro')
        self.assertEqual(self.save(name='Obsoleto', revision=revision).status_code, 409)
        db = self.db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sponsors WHERE name='Obsoleto'").fetchone()[0], 0)
        db.close()

    def test_invalid_upload_url_missing_id_do_not_change_catalog(self):
        self.login(self.manager)
        for values in ({'link_url': 'javascript:alert(1)'}, {'logo': (BytesIO(b'<svg/>'), 'logo.png')}, {'name': ''}):
            self.save(**values)
            self.assertEqual(self.revision(), 1)
        self.assertEqual(self.save(sponsor_id='999').status_code, 404)
        self.assertEqual(self.revision(), 1)

    def test_metadata_cache_and_missing_table_do_not_break_transaction(self):
        invalidate_cache()
        db = self.db()
        statements = []
        db.conn.set_trace_callback(statements.append)
        public_sponsors(db, 'isolated-cache-test')
        public_sponsors(db, 'isolated-cache-test')
        self.assertEqual(sum('FROM sponsors' in sql for sql in statements), 1)
        db.execute('DROP TABLE sponsors')
        self.assertEqual(public_sponsors(db, 'isolated-cache-missing'), [])
        self.assertEqual(db.execute('SELECT revision FROM sponsor_catalog').fetchone()[0], 1)
        db.rollback(); db.close()

    def test_offline_route_does_not_load_footer_database(self):
        with patch('src.routes.sponsors.get_db', side_effect=AssertionError('offline must not access db')):
            self.assertEqual(self.client.get('/offline').status_code, 200)

    def test_template_context_outside_http_never_queries_sponsors(self):
        from src.routes.sponsors import footer_context
        with pages.app.app_context(), patch('src.routes.sponsors.get_db', side_effect=AssertionError('not an HTTP template')):
            self.assertEqual(footer_context()['footer_sponsors'], [])

    def test_all_other_roles_are_forbidden(self):
        for role in ('staff', 'client', 'football_manager', 'infra', 'maintenance', 'display'):
            db = self.db()
            with db: db.execute('UPDATE users SET role=? WHERE id=?', (role, self.staff))
            db.close()
            self.login(self.staff)
            self.assertEqual(self.client.get('/admin/sponsors', headers={'Accept': 'application/json'}).status_code, 403)

    def test_cache_expiry_and_write_invalidation(self):
        self.login(self.manager)
        self.add_logo()
        db = self.db()
        invalidate_cache()
        namespace = 'cache-expiry-test'
        self.assertEqual(len(public_sponsors(db, namespace)), 1)
        with db: db.execute('UPDATE sponsors SET active=0 WHERE primary_slot IS NULL')
        self.assertEqual(len(public_sponsors(db, namespace)), 1)
        with patch('src.services.sponsors.time.monotonic', return_value=10**20):
            self.assertEqual(public_sponsors(db, namespace), [])
        db.close()
        # A manager mutation clears the per-instance cache immediately.
        self.add_logo()
        db = self.db()
        self.assertEqual(len(public_sponsors(db, namespace)), 1)
        db.close()

    def test_sqlite_revision_race_and_primary_constraint(self):
        def attempt(_):
            db = self.db()
            try:
                with db: return db.execute('UPDATE sponsor_catalog SET revision=revision+1 WHERE revision=1').rowcount
            finally: db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [0, 1])
        import sqlite3
        db = self.db()
        with self.assertRaises(sqlite3.IntegrityError):
            with db: db.execute("INSERT INTO sponsors(name,primary_slot) VALUES('Duplicado',1)")
        db.close()


class SponsorAssetsTest(unittest.TestCase):
    def test_image_optimized_and_transparency_preserved(self):
        data, version = process_logo(FileStorage(stream=BytesIO(png()), filename='logo.png', content_type='image/png'))
        self.assertLessEqual(len(data), 131072)
        self.assertEqual(len(version), 64)
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'WEBP')
            self.assertIn('A', image.mode)
            self.assertFalse(image.getexif())

    def test_reject_mime_extension_content_size_and_resolution(self):
        huge = BytesIO(); Image.new('RGB', (8001, 1)).save(huge, format='PNG')
        for raw, name, mime in [(png(), 'logo.svg', 'image/png'), (png(), 'logo.png', 'image/jpeg'),
                                (b'invalid', 'logo.png', 'image/png'), (b'x'*(2*1024*1024+1), 'logo.png', 'image/png'),
                                (huge.getvalue(), 'logo.png', 'image/png')]:
            with self.assertRaises(ValueError):
                process_logo(FileStorage(stream=BytesIO(raw), filename=name, content_type=mime))

    def test_links(self):
        for link in ['javascript:alert(1)', '//example.com', 'http://example.com', 'https://user:pass@example.com',
                     'https://localhost', 'https://127.0.0.1', 'https://example.com:888', 'https://example.com\\@bad.com',
                     'https://example.com/\nattack', 'data:text/html,bad', 'https://0177.0.0.1', 'https://127.1',
                     'https://example%0a.com', 'https://-bad.example.com']:
            with self.assertRaises(ValueError, msg=repr(link)): validate_link(link)
        self.assertEqual(validate_link(' https://example.com/sponsor?q=1 '), 'https://example.com/sponsor?q=1')
        self.assertEqual(validate_link(''), '')

    def test_animation_and_corrupted_image_are_rejected(self):
        animated = BytesIO()
        Image.new('RGB', (10, 10), 'red').save(animated, format='WEBP', save_all=True,
            append_images=[Image.new('RGB', (10, 10), 'blue')], duration=100, loop=0)
        for raw, filename, mime in [(animated.getvalue(), 'logo.webp', 'image/webp'), (png()[:45], 'logo.png', 'image/png')]:
            with self.assertRaises(ValueError):
                process_logo(FileStorage(stream=BytesIO(raw), filename=filename, content_type=mime))


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'disposable PostgreSQL required')
class SponsorPostgresTest(unittest.TestCase):
    def setUp(self):
        import psycopg2
        from psycopg2.extras import DictCursor
        socket = os.environ['GPCTA_TEST_POSTGRES_SOCKET']
        assert socket.startswith('/tmp/gpcta-pg.')
        self.args = dict(host=socket, port=55439, dbname='postgres', cursor_factory=DictCursor)
        self.schema = 'sponsors_' + uuid.uuid4().hex
        self.admin = psycopg2.connect(**self.args); self.admin.autocommit = True
        with self.admin.cursor() as cursor: cursor.execute('CREATE SCHEMA '+self.schema)
        self.addCleanup(self.cleanup)
        db = self.connect()
        with db:
            db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY)')
            db.execute(Path('supabase/migrations/20261018050000_sponsors.sql').read_text())
        db.close()

    def connect(self):
        import psycopg2
        db = DbWrapper(psycopg2.connect(**self.args), True)
        db.execute('SET search_path TO '+self.schema); db.commit()
        return db

    def cleanup(self):
        with self.admin.cursor() as cursor: cursor.execute('DROP SCHEMA '+self.schema+' CASCADE')
        self.admin.close()

    def test_migration_reapply_rls_and_binary_persistence(self):
        self.assertTrue(Path('supabase/migrations/20261018050000_sponsors.sql').read_text().endswith(schema_sql(True)))
        db = self.connect()
        with db:
            db.execute(schema_sql(True))
            raw, version = process_logo(FileStorage(stream=BytesIO(png()), filename='logo.png', content_type='image/png'))
            db.execute('UPDATE sponsors SET logo_data=?,logo_version=?,active=1 WHERE primary_slot=1', (raw, version))
        db.close()
        db = self.connect()
        self.assertEqual(bytes(db.execute('SELECT logo_data FROM sponsors WHERE primary_slot=1').fetchone()[0]), raw)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors').fetchone()[0], 2)
        protected = db.execute("SELECT relrowsecurity FROM pg_class WHERE relnamespace=(SELECT oid FROM pg_namespace WHERE nspname=current_schema()) AND relname IN ('sponsors','sponsor_catalog')").fetchall()
        self.assertEqual(len(protected), 2); self.assertTrue(all(row[0] for row in protected))
        rows = public_sponsors(db, self.schema)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['logo_version'], version)
        self.assertNotIn('logo_data', rows[0])
        db.execute('DROP TABLE sponsors')
        self.assertEqual(public_sponsors(db, self.schema+'-missing'), [])
        self.assertEqual(db.execute('SELECT revision FROM sponsor_catalog').fetchone()[0], 1)
        db.rollback()
        db.close()

    def test_concurrent_revision_only_one_winner(self):
        def attempt(_):
            db = self.connect()
            try:
                with db:
                    return db.execute('UPDATE sponsor_catalog SET revision=revision+1 WHERE id=1 AND revision=1').rowcount
            finally: db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [0, 1])
        # Exercise the actual batched route SQL with native PostgreSQL types.
        from flask import g
        from src.routes.sponsors import reorder
        db = self.connect()
        with db:
            db.execute('INSERT INTO users(id) VALUES(1)')
            first = db.execute("INSERT INTO sponsors(name) VALUES('Primeiro')").lastrowid
            second = db.execute("INSERT INTO sponsors(name) VALUES('Segundo')").lastrowid
        with pages.app.test_request_context('/admin/sponsors/order', method='POST', json={'revision': 2, 'ids': [second, first]}), patch('src.routes.sponsors.get_db', return_value=db):
            g.user = {'id': 1, 'role': 'manager'}
            self.assertEqual(reorder().status_code, 200)
        db.close()
        db = self.connect()
        self.assertEqual([row[0] for row in db.execute('SELECT id FROM sponsors WHERE primary_slot IS NULL ORDER BY sort_order').fetchall()], [second, first])
        db.close()

    def test_unique_primary_slot_and_rollback(self):
        import psycopg2
        db = self.connect()
        with self.assertRaises(psycopg2.IntegrityError):
            with db:
                db.execute('UPDATE sponsor_catalog SET revision=revision+1 WHERE id=1')
                db.execute("INSERT INTO sponsors(name,primary_slot) VALUES('Duplicado',1)")
        self.assertEqual(db.execute('SELECT revision FROM sponsor_catalog').fetchone()[0], 1)
        db.close()

    def test_migration_ddl_is_transactional(self):
        db = self.connect()
        with db:
            db.execute('DROP TABLE sponsors')
            db.execute('DROP TABLE sponsor_catalog')
        with self.assertRaises(RuntimeError):
            with db:
                db.execute(Path('supabase/migrations/20261018050000_sponsors.sql').read_text())
                raise RuntimeError('simulate failure before migration ledger commit')
        self.assertIsNone(db.execute("SELECT to_regclass('sponsors')").fetchone()[0])
        self.assertIsNone(db.execute("SELECT to_regclass('sponsor_catalog')").fetchone()[0])
        db.close()
