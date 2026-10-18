from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, flash, g, has_request_context, jsonify, redirect, render_template, request, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.sponsors import invalidate_cache, process_logo, public_sponsors, validate_link

bp = Blueprint('sponsors', __name__)


@bp.app_context_processor
def footer_context():
    if not has_request_context() or request.endpoint in {'offline', 'service_worker', 'static', 'health'}:
        return {'footer_sponsors': [], 'copyright_year': datetime.now(timezone.utc).year}
    namespace = repr(tuple(current_app.config.get(key) for key in ('APP_ENV', 'DATABASE_URL', 'DATABASE')))
    rows = public_sponsors(get_db(), namespace)
    return {'footer_sponsors': rows, 'copyright_year': datetime.now(timezone.utc).year}


def claim_revision(db, value):
    try:
        revision = int(value)
    except (TypeError, ValueError):
        abort(400, 'Revisão inválida.')
    cursor = db.execute('UPDATE sponsor_catalog SET revision=revision+1 WHERE id=1 AND revision=?', (revision,))
    if cursor.rowcount != 1:
        abort(409, 'O cadastro mudou. Recarregue a página antes de salvar.')


@bp.route('/admin/sponsors', methods=['GET', 'POST'])
@roles_allowed('manager')
def manage():
    db = get_db()
    if request.method == 'POST':
        if request.content_length and request.content_length > 3 * 1024 * 1024:
            abort(413)
        try:
            name = request.form.get('name', '').strip()
            if not 1 <= len(name) <= 100:
                raise ValueError('Informe um nome de até 100 caracteres para identificação e texto alternativo.')
            link = validate_link(request.form.get('link_url'))
            upload = request.files.get('logo')
            image = process_logo(upload) if upload and upload.filename else None
            remove = request.form.get('remove_logo') == '1'
            if image and remove:
                raise ValueError('Escolha enviar ou remover a logomarca.')
            active = int(request.form.get('active') == '1')
            sponsor_id = request.form.get('sponsor_id')
            with db:
                claim_revision(db, request.form.get('revision'))
                if sponsor_id:
                    try:
                        sponsor_id = int(sponsor_id)
                    except ValueError:
                        abort(400)
                    row = db.execute('SELECT id FROM sponsors WHERE id=?', (sponsor_id,)).fetchone()
                    if row is None:
                        abort(404)
                    db.execute('''UPDATE sponsors SET name=?,link_url=?,active=?,updated_at=?,updated_by=?
                        WHERE id=?''', (name, link, active, datetime.now(timezone.utc).isoformat(), g.user['id'], sponsor_id))
                else:
                    count = db.execute('SELECT COUNT(*) AS total FROM sponsors WHERE primary_slot IS NULL').fetchone()['total']
                    if count >= 100:
                        raise ValueError('Limite de 100 patrocinadores comuns atingido.')
                    sponsor_id = db.execute('''INSERT INTO sponsors(name,link_url,active,sort_order,updated_by)
                        VALUES(?,?,?,?,?)''', (name, link, active, count, g.user['id'])).lastrowid
                if image or remove:
                    db.execute('UPDATE sponsors SET logo_data=?,logo_version=? WHERE id=?',
                               (*image, sponsor_id) if image else (None, None, sponsor_id))
            invalidate_cache()
            flash('Patrocinador salvo. Sem logomarca, ele não aparece no rodapé.', 'success')
            return redirect(url_for('sponsors.manage'))
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'danger')
            return redirect(url_for('sponsors.manage'))
    rows = [dict(row) for row in db.execute('''SELECT id,name,link_url,primary_slot,active,sort_order,logo_version
        FROM sponsors ORDER BY CASE WHEN primary_slot IS NULL THEN 1 ELSE 0 END,primary_slot,sort_order,id''').fetchall()]
    revision = db.execute('SELECT revision FROM sponsor_catalog WHERE id=1').fetchone()['revision']
    response = current_app.make_response(render_template('sponsors.html', sponsors=rows, revision=revision))
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@bp.post('/admin/sponsors/order')
@roles_allowed('manager')
def reorder():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('ids'), list):
        abort(400)
    ids = data['ids']
    if len(ids) > 100 or any(type(item) is not int for item in ids) or len(set(ids)) != len(ids):
        abort(400)
    db = get_db()
    with db:
        claim_revision(db, data.get('revision'))
        expected = {row['id'] for row in db.execute('SELECT id FROM sponsors WHERE primary_slot IS NULL').fetchall()}
        if set(ids) != expected:
            abort(400, 'Envie todos os patrocinadores comuns, sem as posições principais.')
        if ids:
            cases = ' '.join('WHEN ? THEN ?' for _ in ids)
            params = [value for position, sponsor_id in enumerate(ids) for value in (sponsor_id, position)]
            params += [datetime.now(timezone.utc).isoformat(), g.user['id'], *ids]
            db.execute('UPDATE sponsors SET sort_order=CASE id '+cases+' END,updated_at=?,updated_by=? '
                       'WHERE primary_slot IS NULL AND id IN ('+','.join('?' for _ in ids)+')', params)
    invalidate_cache()
    return jsonify(ok=True, revision=int(data['revision']) + 1)


@bp.get('/sponsors/<int:sponsor_id>/logo/<version>')
def logo(sponsor_id, version):
    if len(version) != 64 or any(char not in '0123456789abcdef' for char in version):
        abort(404)
    row = get_db().execute('''SELECT logo_data FROM sponsors
        WHERE id=? AND logo_version=? AND active=1 AND logo_data IS NOT NULL''', (sponsor_id, version)).fetchone()
    if row is None:
        abort(404)
    response = Response(bytes(row['logo_data']), mimetype='image/webp')
    response.headers['Cache-Control'] = 'public, max-age=60, s-maxage=60'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.set_etag(version)
    return response.make_conditional(request)
