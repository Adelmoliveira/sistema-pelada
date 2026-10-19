from html.parser import HTMLParser
import os
from pathlib import Path
import unittest

import test_sponsors as fixtures
from src.db import initialize_sqlite_database
from src.services.sponsors import instagram_profile_url, normalize_instagram, public_sponsors


class InstagramPagesTest(unittest.TestCase):
    setUp = fixtures.SponsorPagesTest.setUp
    login = fixtures.SponsorPagesTest.login
    db = fixtures.SponsorPagesTest.db
    revision = fixtures.SponsorPagesTest.revision
    save = fixtures.SponsorPagesTest.save
    add_logo = fixtures.SponsorPagesTest.add_logo
    latest = fixtures.SponsorPagesTest.latest

    def test_all_destination_combinations_and_safe_markup(self):
        self.login(self.manager)
        for name, site, instagram in [('Site', 'https://example.com/site', ''), ('Instagram', '', '@Perfil.CTA'),
                                       ('Ambos', 'https://example.com/ambos', 'perfil_duplo'), ('Nenhum', '', '')]:
            self.add_logo(name=name, link_url=site, instagram=instagram)
        self.client.post('/logout')
        html = self.client.get('/login').get_data(as_text=True)
        class Links(HTMLParser):
            def __init__(self): super().__init__(); self.groups=[]; self.group=None
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if tag == 'span' and 'data-sponsor-logo' in attrs:
                    self.group=[]; self.groups.append(self.group)
                if self.group is not None and tag == 'a': self.group.append(attrs)
            def handle_endtag(self, tag):
                if tag == 'span': self.group=None
        parser=Links(); parser.feed(html)
        self.assertEqual([[a['href'] for a in group] for group in parser.groups], [
            ['https://example.com/site'], ['https://www.instagram.com/perfil.cta/'],
            ['https://example.com/ambos', 'https://www.instagram.com/perfil_duplo/'], []])
        for group in parser.groups:
            for anchor in group:
                self.assertEqual(anchor['target'], '_blank')
                self.assertTrue({'noopener','noreferrer'}.issubset(anchor['rel'].split()))
        self.assertIn('aria-label="Instagram de Ambos (abre em nova aba)"', html)
        self.assertIn('aria-label="Instagram de Instagram (abre em nova aba)"', html)
        self.assertIn('aria-label="Site de Ambos (abre em nova aba)"', html)
        self.assertEqual(html.count('class="sponsor-instagram-link"'), 1)
        self.assertNotIn('>Instagram de Ambos<', html)
        self.assertEqual(html.count('Copyright © 2026 | Grupo de Peladas do CTA - GPCTA'), 1)

    def test_edit_clear_and_legacy_forms_preserve_existing_links(self):
        self.login(self.manager)
        self.add_logo(link_url='https://example.com/original', instagram='@Original.CTA')
        row=self.latest()
        self.assertEqual(row['instagram_username'],'original.cta')
        self.save(sponsor_id=row['id'],name='Nome alterado')
        edited=self.latest()
        self.assertEqual(edited['instagram_username'],'original.cta')
        self.assertEqual(edited['link_url'],'https://example.com/original')
        self.assertEqual(edited['logo_version'],row['logo_version'])
        self.save(sponsor_id=row['id'],instagram='https://instagram.com/Outro_123/?igsh=abc')
        self.assertEqual(self.latest()['instagram_username'],'outro_123')
        self.save(sponsor_id=row['id'],instagram='',link_url='')
        self.assertEqual((self.latest()['instagram_username'],self.latest()['link_url']),('',''))

    def test_invalid_input_does_not_mutate_record_or_revision(self):
        self.login(self.manager); self.save(link_url='https://example.com/existente')
        original=self.latest(); revision=self.revision()
        for value in ('javascript:alert(1)','https://instagram.com.evil.test/perfil/','https://instagram.com/p/abc/'):
            self.save(sponsor_id=original['id'],name='Não salvar',instagram=value)
            self.assertEqual(self.revision(),revision)
            self.assertEqual(self.latest()['name'],original['name'])
            self.assertEqual(self.latest()['link_url'],original['link_url'])

    def test_sqlite_upgrade_and_reinitialization_preserve_legacy_data(self):
        self.login(self.manager); self.add_logo(link_url='https://example.com/legado')
        before=self.latest()
        db=self.db()
        with db: db.execute('ALTER TABLE sponsors DROP COLUMN instagram_username')
        db.close()
        initialize_sqlite_database(fixtures.pages.app.config['DATABASE'])
        after=self.latest()
        self.assertEqual(after['instagram_username'],'')
        for key,value in before.items():
            if key!='instagram_username': self.assertEqual(after[key],value)
        self.save(sponsor_id=after['id'],instagram='@persistente')
        initialize_sqlite_database(fixtures.pages.app.config['DATABASE'])
        self.assertEqual(self.latest()['instagram_username'],'persistente')

    def test_instagram_is_manager_only_and_csrf_protected(self):
        for user in (self.staff, self.client_user):
            self.login(user)
            self.assertEqual(self.client.post('/admin/sponsors',data={'name':'Negado','instagram':'@teste','revision':1},headers={'Accept':'application/json'}).status_code,403)
        self.login(self.manager)
        fixtures.pages.app.config['WTF_CSRF_ENABLED']=True
        self.assertEqual(self.save(instagram='@sem_csrf').status_code,303)
        self.assertEqual(self.revision(),1)

    def test_cache_position_and_unsafe_direct_data_preserve_site(self):
        from src.services.sponsors import invalidate_cache
        self.login(self.manager)
        self.add_logo(sponsor_id=1,name='VINAC',link_url='https://example.com/principal',instagram='@antigo')
        db=self.db()
        self.assertEqual(public_sponsors(db,'instagram-cache')[0]['instagram_url'],'https://www.instagram.com/antigo/')
        self.save(sponsor_id=1,name='VINAC',instagram='@novo')
        row=public_sponsors(db,'instagram-cache')[0]
        self.assertEqual(row['instagram_url'],'https://www.instagram.com/novo/')
        self.assertEqual(row['primary_slot'],1)
        self.assertEqual(row['link_url'],'https://example.com/principal')
        with db: db.execute("UPDATE sponsors SET instagram_username='javascript:alert(1)' WHERE id=1")
        invalidate_cache()
        row=public_sponsors(db,'instagram-cache')[0]
        self.assertEqual(row['instagram_url'],'')
        self.assertEqual(row['link_url'],'https://example.com/principal')
        db.close()


class InstagramValidationTest(unittest.TestCase):
    def test_normalization(self):
        for value in ('@Usuario.CTA','Usuario.CTA','https://instagram.com/Usuario.CTA',
                      'https://www.instagram.com/Usuario.CTA/?igsh=abc#tracking','https://m.instagram.com/Usuario.CTA/'):
            self.assertEqual(normalize_instagram(value),'usuario.cta')
            self.assertEqual(instagram_profile_url(value),'https://www.instagram.com/usuario.cta/')
        self.assertEqual(normalize_instagram('  '),'')
        self.assertEqual(instagram_profile_url(''),'')
        self.assertEqual(normalize_instagram('a'*30),'a'*30)

    def test_unsafe_hosts_non_profiles_and_invalid_names(self):
        for value in ('a'*31,'@@perfil','nome com espaço','usuario/extra','.nome','nome.','nome..cta',
                      'javascript:alert(1)','https://instagram.com.evil.test/perfil/',
                      'https://instagram.com@evil.test/perfil/','https://evil.test@instagram.com/perfil/',
                      'https://instagram.com:444/perfil/','http://instagram.com/perfil/',
                      'https://instagram.com/p/abc/','https://instagram.com/reel/abc/',
                      'https://instagram.com/accounts/','https://instagram.com/perfil%2fextra/',
                      'https://instagram.com/perfil/\nattack','https://instagram.com\\@evil.test/perfil/'):
            with self.assertRaises(ValueError,msg=repr(value)): normalize_instagram(value)


@unittest.skipUnless(os.environ.get('GPCTA_TEST_POSTGRES_SOCKET'), 'disposable PostgreSQL required')
class InstagramPostgresTest(unittest.TestCase):
    setUp = fixtures.SponsorPostgresTest.setUp
    connect = fixtures.SponsorPostgresTest.connect
    cleanup = fixtures.SponsorPostgresTest.cleanup

    def test_migration_preserves_data_and_is_repeatable(self):
        db=self.connect()
        with db:
            db.execute("UPDATE sponsors SET link_url='https://example.com/legado',active=1 WHERE id=1")
            db.execute('ALTER TABLE sponsors DROP COLUMN instagram_username')
        before=[dict(row) for row in db.execute('SELECT * FROM sponsors ORDER BY id').fetchall()]
        migration=Path('supabase/migrations/20261018060000_sponsors_instagram.sql').read_text()
        with db:
            db.execute(migration); db.execute(migration)
        db.close(); db=self.connect()
        after=[dict(row) for row in db.execute('SELECT * FROM sponsors ORDER BY id').fetchall()]
        self.assertEqual(len(before),len(after))
        for old,new in zip(before,after):
            self.assertEqual(new.pop('instagram_username'),''); self.assertEqual(old,new)
        with db:
            db.execute("UPDATE sponsors SET instagram_username='perfil_novo' WHERE id=1")
            db.execute(migration)
        self.assertEqual(db.execute('SELECT instagram_username FROM sponsors WHERE id=1').fetchone()[0],'perfil_novo')
        db.close()

    def test_rollback_and_public_url(self):
        db=self.connect()
        with db: db.execute('ALTER TABLE sponsors DROP COLUMN instagram_username')
        with self.assertRaises(RuntimeError):
            with db:
                db.execute(Path('supabase/migrations/20261018060000_sponsors_instagram.sql').read_text())
                raise RuntimeError('rollback before migration history commit')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='sponsors' AND column_name='instagram_username'").fetchone()[0],0)
        with db:
            db.execute(Path('supabase/migrations/20261018060000_sponsors_instagram.sql').read_text())
            raw,version=fixtures.process_logo(fixtures.FileStorage(stream=fixtures.BytesIO(fixtures.png()),filename='logo.png',content_type='image/png'))
            db.execute('UPDATE sponsors SET active=1,logo_data=?,logo_version=?,instagram_username=? WHERE id=1',(raw,version,'perfil.cta'))
        rows=public_sponsors(db,self.schema+'-instagram')
        self.assertEqual(rows[0]['instagram_url'],'https://www.instagram.com/perfil.cta/')
        db.close()
