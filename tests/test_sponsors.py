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
        self.assertEqual(self.client.post('/admin/sponsors/preview', data={}).status_code, 303)
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
        self.assertLess(footer.index('<img'), footer.index('Copyright © 2026'))
        self.assertEqual(html.count('Copyright © 2026 | Grupo de Peladas do CTA - GPCTA'), 1)
        self.assertNotIn('© 2026 GPCTA', html)
        self.assertIn('APOIADORES GPCTA', footer)

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

    def test_delete_requires_manager_confirmation_and_revision(self):
        self.login(self.manager); self.add_logo()
        row = self.latest()
        path = '/admin/sponsors/{}/delete'.format(row['id'])
        for user in (self.staff, self.client_user):
            self.login(user)
            self.assertEqual(self.client.post(path, data={'revision': self.revision(), 'confirm_delete': '1'}, headers={'Accept': 'application/json'}).status_code, 403)
        self.login(self.manager)
        self.assertIn('Confirmar exclusão', self.client.get(path).get_data(as_text=True))
        revision = self.revision()
        self.assertEqual(self.client.post(path, data={'revision': revision}).status_code, 400)
        self.assertEqual(self.client.post(path, data={'revision': revision-1, 'confirm_delete': '1'}).status_code, 409)
        self.assertEqual(self.client.post(path, data={'revision': revision, 'confirm_delete': '1'}).status_code, 302)
        db = self.db()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors WHERE id=?', (row['id'],)).fetchone()[0], 0)
        db.close()
        self.assertEqual(self.client.get('/sponsors/{}/logo/{}'.format(row['id'], row['logo_version'])).status_code, 404)
        self.client.post('/logout')
        self.assertNotIn('class="sponsor-logos"', self.client.get('/login').get_data(as_text=True))

    def test_primary_deletion_releases_only_its_slot_and_delete_csrf(self):
        self.login(self.manager)
        self.add_logo(sponsor_id=1, name='VINAC')
        self.save(name='Comum')
        path = '/admin/sponsors/1/delete'
        confirmation = self.client.get(path).get_data(as_text=True)
        self.assertIn('VINAC', confirmation)
        self.assertIn('posição principal 1', confirmation)
        self.assertEqual(self.client.post(path, data={'revision': self.revision(), 'confirm_delete': '1'}).status_code, 302)
        db = self.db()
        self.assertEqual([tuple(row) for row in db.execute('SELECT name,primary_slot,sort_order FROM sponsors ORDER BY id').fetchall()], [('Prudention', 2, 0), ('Comum', None, 0)])
        db.close()
        # Repeated local bootstrap must not resurrect a deleted principal.
        from src.db import initialize_sqlite_database
        initialize_sqlite_database(pages.app.config['DATABASE'])
        db = self.db(); self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors WHERE primary_slot=1').fetchone()[0], 0); db.close()
        self.assertIn('Posição principal 1 disponível', self.client.get('/admin/sponsors').get_data(as_text=True))
        self.save(); row = self.latest()
        pages.app.config['WTF_CSRF_ENABLED'] = True
        self.assertEqual(self.client.post('/admin/sponsors/{}/delete'.format(row['id']), data={'revision': self.revision(), 'confirm_delete': '1'}).status_code, 303)

    def test_edit_preserves_identity_position_and_replaces_or_removes_image(self):
        self.login(self.manager); self.add_logo(sponsor_id=2, name='Prudention')
        db = self.db(); original = dict(db.execute('SELECT * FROM sponsors WHERE id=2').fetchone()); db.close()
        self.save(sponsor_id=2, name='Nome atualizado', link_url='https://example.com/apoio', active='0')
        db = self.db(); edited = dict(db.execute('SELECT * FROM sponsors WHERE id=2').fetchone()); db.close()
        self.assertEqual((edited['id'], edited['primary_slot'], edited['logo_version']), (2, 2, original['logo_version']))
        self.assertEqual((edited['name'], edited['link_url'], edited['active']), ('Nome atualizado', 'https://example.com/apoio', 0))
        self.save(sponsor_id=2, name='Nome atualizado', logo=(BytesIO(bordered_png()), 'new.png'))
        db = self.db(); changed = dict(db.execute('SELECT * FROM sponsors WHERE id=2').fetchone()); db.close()
        self.assertNotEqual(changed['logo_version'], original['logo_version'])
        self.assertEqual((changed['id'], changed['primary_slot'], changed['active']), (2, 2, 1))
        self.save(sponsor_id=2, name='Nome atualizado', remove_logo='1')
        db = self.db(); removed = dict(db.execute('SELECT * FROM sponsors WHERE id=2').fetchone()); db.close()
        self.assertIsNone(removed['logo_data']); self.assertIsNone(removed['logo_version'])
        self.assertEqual(removed['primary_slot'], 2)

    def test_actions_have_icons_and_all_roles_cannot_delete_principals(self):
        self.login(self.manager)
        html = self.client.get('/admin/sponsors').get_data(as_text=True)
        for name in ('VINAC', 'Prudention'):
            self.assertIn('aria-label="Editar '+name+'"', html)
            self.assertIn('aria-label="Excluir '+name+'"', html)
        self.assertIn('<svg', html)
        for role in ('staff', 'client', 'football_manager', 'infra', 'maintenance', 'display'):
            db = self.db()
            with db: db.execute('UPDATE users SET role=? WHERE id=?', (role, self.staff))
            db.close(); self.login(self.staff)
            self.assertEqual(self.client.post('/admin/sponsors/2/delete', data={'confirm_delete': '1', 'revision': 1}, headers={'Accept': 'application/json'}).status_code, 403)
        self.assertEqual(self.revision(), 1)

    def test_delete_both_principals_keeps_common_order_and_does_not_promote(self):
        self.login(self.manager)
        self.save(name='A'); self.save(name='B')
        for sponsor_id in (1, 2):
            self.assertEqual(self.client.post('/admin/sponsors/{}/delete'.format(sponsor_id), data={'revision': self.revision(), 'confirm_delete': '1'}).status_code, 302)
        db = self.db()
        self.assertEqual([tuple(row) for row in db.execute('SELECT name,primary_slot,sort_order FROM sponsors ORDER BY sort_order,id').fetchall()], [('A', None, 0), ('B', None, 1)])
        db.close()

    def test_delete_then_create_appends_after_remaining_sponsors(self):
        self.login(self.manager)
        for name in ('A', 'B', 'C'): self.save(name=name)
        db = self.db(); second = db.execute("SELECT id FROM sponsors WHERE name='B'").fetchone()[0]; db.close()
        self.client.post('/admin/sponsors/{}/delete'.format(second), data={'revision': self.revision(), 'confirm_delete': '1'})
        self.save(name='D')
        db = self.db()
        self.assertEqual([row[0] for row in db.execute('SELECT name FROM sponsors WHERE primary_slot IS NULL ORDER BY sort_order,id').fetchall()], ['A', 'C', 'D'])
        db.close()

    def test_private_preview_inactive_and_preview_does_not_persist(self):
        self.login(self.manager); self.add_logo(active='0'); row = self.latest()
        revision = self.revision()
        preview_path = '/admin/sponsors/{}/logo'.format(row['id'])
        self.assertIn('no-store', self.client.get(preview_path).headers['Cache-Control'])
        response = self.client.post('/admin/sponsors/preview', data={'sponsor_id': row['id']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/webp')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.revision(), revision)
        self.login(self.staff)
        self.assertEqual(self.client.get(preview_path, headers={'Accept': 'application/json'}).status_code, 403)
        self.assertEqual(self.client.post('/admin/sponsors/preview', headers={'Accept': 'application/json'}).status_code, 403)

    def test_background_removal_requires_explicit_confirmation(self):
        self.login(self.manager)
        self.save(logo=(BytesIO(bordered_png()), 'logo.png'), remove_background='1')
        self.assertEqual(self.revision(), 1)
        response = self.client.post('/admin/sponsors/preview', data={'logo': (BytesIO(bordered_png()), 'logo.png'), 'remove_background': '1', 'confirm_background': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.revision(), 1)
        self.save(logo=(BytesIO(bordered_png()), 'logo.png'), remove_background='1', confirm_background='1')
        self.assertEqual(self.revision(), 2)
        with Image.open(BytesIO(self.latest()['logo_data'])) as image:
            self.assertEqual(image.size, (51, 51))

    def test_existing_logo_background_can_be_previewed_without_mutating(self):
        self.login(self.manager)
        self.save(logo=(BytesIO(bordered_png()), 'logo.png'))
        original = self.latest()
        response = self.client.post('/admin/sponsors/preview', data={'sponsor_id': original['id'], 'remove_background': '1', 'confirm_background': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.latest()['logo_version'], original['logo_version'])
        self.assertEqual(self.revision(), 2)
        self.save(sponsor_id=original['id'], remove_background='1', confirm_background='1')
        self.assertEqual(self.latest()['id'], original['id'])
        self.assertNotEqual(self.latest()['logo_version'], original['logo_version'])
        with Image.open(BytesIO(self.latest()['logo_data'])) as image:
            self.assertLess(max(image.size), 80)

    def test_shared_copyright_and_title_on_login_and_other_pages(self):
        self.login(self.manager); self.add_logo()
        for path in ('/login', '/admin/sponsors'):
            if path == '/login': self.client.post('/logout')
            else: self.login(self.manager)
            html = self.client.get(path).get_data(as_text=True)
            self.assertEqual(html.count('Copyright © 2026 | Grupo de Peladas do CTA - GPCTA'), 1)
            self.assertEqual(html.count('APOIADORES GPCTA'), 1)
            self.assertLess(html.index('APOIADORES GPCTA'), html.index('Copyright © 2026'))
        css = Path('static/sponsors.css').read_text()
        self.assertIn('height: 52px', css); self.assertIn('height: 36px', css)
        self.assertIn('100dvh', css); self.assertIn('safe-area-inset-bottom', css)
        self.assertNotIn('overflow-y', css)


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

    def test_uniform_background_removal_is_connected_and_conservative(self):
        from src.services.sponsors import remove_uniform_background
        with Image.open(BytesIO(bordered_png())) as image:
            result = remove_uniform_background(image.convert('RGBA'))
        self.assertEqual(result.getpixel((0, 0))[3], 0)
        # Enclosed white brand detail must not be mistaken for exterior background.
        self.assertEqual(result.getpixel((40, 40)), (255, 255, 255, 255))
        self.assertEqual(result.getpixel((20, 20)), (0, 60, 120, 255))
        gradient = Image.new('RGBA', (80, 80), 'white')
        gradient.putpixel((0, 0), (0, 0, 0, 255))
        with self.assertRaises(ValueError): remove_uniform_background(gradient)
        with self.assertRaises(ValueError): remove_uniform_background(Image.new('RGBA', (80, 80), 'white'))


def bordered_png():
    from PIL import ImageDraw
    image = Image.new('RGB', (80, 80), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 15, 65, 65), fill=(0, 60, 120))
    draw.rectangle((30, 30, 50, 50), fill='white')
    stream = BytesIO(); image.save(stream, format='PNG'); return stream.getvalue()


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
        raw, version = process_logo(FileStorage(stream=BytesIO(png()), filename='logo.png', content_type='image/png'))
        with db: db.execute('UPDATE sponsors SET logo_data=?,logo_version=? WHERE id=?', (raw, version, first))
        from src.routes.sponsors import delete
        with pages.app.test_request_context('/admin/sponsors/{}/delete'.format(first), method='POST', data={'revision': 3, 'confirm_delete': '1'}), patch('src.routes.sponsors.get_db', return_value=db):
            g.user = {'id': 1, 'role': 'manager'}
            self.assertEqual(delete(first).status_code, 302)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors WHERE id=?', (first,)).fetchone()[0], 0)
        with pages.app.test_request_context('/admin/sponsors/1/delete', method='POST', data={'revision': 4, 'confirm_delete': '1'}), patch('src.routes.sponsors.get_db', return_value=db):
            g.user = {'id': 1, 'role': 'manager'}
            self.assertEqual(delete(1).status_code, 302)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors WHERE primary_slot=1').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT primary_slot FROM sponsors WHERE id=2').fetchone()[0], 2)
        with db: db.execute("INSERT INTO sponsors(name,primary_slot) VALUES('Nova referência de teste',1)")
        self.assertEqual(db.execute('SELECT COUNT(*) FROM sponsors WHERE primary_slot=1').fetchone()[0], 1)
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
