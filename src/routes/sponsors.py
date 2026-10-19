from datetime import datetime, timezone
from io import BytesIO

from werkzeug.datastructures import FileStorage

from flask import Blueprint, Response, abort, current_app, flash, g, has_request_context, jsonify, redirect, render_template, request, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.sponsors import invalidate_cache, normalize_instagram, process_logo, public_sponsors, validate_link

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


def prepared_image(db, *, required=False):
    remove_background = request.form.get('remove_background') == '1'
    if remove_background and request.form.get('confirm_background') != '1':
        raise ValueError('Confirme a remoção do fundo após revisar a pré-visualização.')
    upload = request.files.get('logo')
    if not upload or not upload.filename:
        if not (required or remove_background):
            return None
        try:
            sponsor_id = int(request.form.get('sponsor_id', ''))
        except ValueError:
            raise ValueError('Escolha uma logomarca para pré-visualizar.') from None
        row = db.execute('SELECT logo_data FROM sponsors WHERE id=?', (sponsor_id,)).fetchone()
        if row is None or row['logo_data'] is None:
            raise ValueError('Nenhuma logomarca cadastrada. Envie uma imagem.')
        upload = FileStorage(stream=BytesIO(bytes(row['logo_data'])), filename='logo.webp', content_type='image/webp')
    return process_logo(upload, remove_background=remove_background)


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
            link = validate_link(request.form.get('link_url')) if 'link_url' in request.form else None
            instagram = normalize_instagram(request.form.get('instagram')) if 'instagram' in request.form else None
            image = prepared_image(db)
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
                    db.execute('''UPDATE sponsors SET name=?,link_url=COALESCE(?,link_url),
                        instagram_username=COALESCE(?,instagram_username),active=?,updated_at=?,updated_by=?
                        WHERE id=?''', (name, link, instagram, active, datetime.now(timezone.utc).isoformat(), g.user['id'], sponsor_id))
                else:
                    positions = db.execute('SELECT COUNT(*) AS total,COALESCE(MAX(sort_order),-1)+1 AS next_order FROM sponsors WHERE primary_slot IS NULL').fetchone()
                    if positions['total'] >= 100:
                        raise ValueError('Limite de 100 patrocinadores comuns atingido.')
                    sponsor_id = db.execute('''INSERT INTO sponsors(name,link_url,instagram_username,active,sort_order,updated_by)
                        VALUES(?,?,?,?,?,?)''', (name, link or '', instagram or '', active, positions['next_order'], g.user['id'])).lastrowid
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
    rows = [dict(row) for row in db.execute('''SELECT id,name,link_url,instagram_username,primary_slot,active,sort_order,logo_version
        FROM sponsors ORDER BY CASE WHEN primary_slot IS NULL THEN 1 ELSE 0 END,primary_slot,sort_order,id''').fetchall()]
    revision = db.execute('SELECT revision FROM sponsor_catalog WHERE id=1').fetchone()['revision']
    response = current_app.make_response(render_template('sponsors.html', sponsors=rows, revision=revision))
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@bp.route('/admin/sponsors/<int:sponsor_id>/delete', methods=['GET', 'POST'])
@roles_allowed('manager')
def delete(sponsor_id):
    db = get_db()
    # Name and revision must come from one snapshot: a concurrent rename must
    # invalidate the confirmation instead of deleting an unseen new identity.
    row = db.execute('''SELECT s.id,s.name,s.primary_slot,c.revision FROM sponsors s
        CROSS JOIN sponsor_catalog c WHERE s.id=? AND c.id=1''', (sponsor_id,)).fetchone()
    if row is None:
        abort(404)
    if request.method == 'POST':
        if request.form.get('confirm_delete') != '1':
            abort(400, 'Confirme a exclusão.')
        with db:
            claim_revision(db, request.form.get('revision'))
            result = db.execute('DELETE FROM sponsors WHERE id=?', (sponsor_id,))
            if result.rowcount != 1:
                abort(409)
        invalidate_cache()
        flash('Patrocinador e logomarca excluídos.', 'success')
        return redirect(url_for('sponsors.manage'))
    response = current_app.make_response(render_template('sponsor_delete.html', sponsor=row, revision=row['revision']))
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@bp.get('/admin/sponsors/<int:sponsor_id>/logo')
@roles_allowed('manager')
def admin_logo(sponsor_id):
    row = get_db().execute('SELECT logo_data FROM sponsors WHERE id=?', (sponsor_id,)).fetchone()
    if row is None or row['logo_data'] is None:
        abort(404)
    response = Response(bytes(row['logo_data']), mimetype='image/webp')
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@bp.post('/admin/sponsors/preview')
@roles_allowed('manager')
def preview():
    if request.content_length and request.content_length > 3 * 1024 * 1024:
        abort(413)
    try:
        image, _version = prepared_image(get_db(), required=True)
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    response = Response(image, mimetype='image/webp')
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
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
