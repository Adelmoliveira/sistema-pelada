import uuid

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.load_relation_pdf import build_load_relation_pdf
from src.services.material_photos import process_material_photo
from src.services.pix import generate_qrcode_base64
from src.utils import alphabetical_key, local_today


bp = Blueprint("infra", __name__, url_prefix="/infra")
MAX_LOAD_PHOTOS = 6


def material_form_values():
    description = request.form.get("description", "").strip()
    if not description:
        raise ValueError("A descrição é obrigatória.")
    if len(description) > 500:
        raise ValueError("A descrição deve ter no máximo 500 caracteres.")
    load_sheet = request.form.get("load_sheet", "").strip()
    if len(load_sheet) > 100:
        raise ValueError("O código patrimonial FCG deve ter no máximo 100 caracteres.")
    notes = request.form.get("notes", "").strip()
    if len(notes) > 5000:
        raise ValueError("As observações devem ter no máximo 5.000 caracteres.")
    return description, load_sheet, notes


def material_options(db):
    rows = db.execute("SELECT id,description,load_sheet FROM materials").fetchall()
    return sorted(rows, key=lambda material: alphabetical_key(material["description"]))


def load_form_values(db):
    try:
        material_id = int(request.form.get("material_id", ""))
    except (TypeError, ValueError):
        raise ValueError("Selecione um material.")
    if not db.execute("SELECT 1 FROM materials WHERE id=?", (material_id,)).fetchone():
        raise ValueError("O material selecionado não existe.")
    serial_number = request.form.get("serial_number", "").strip()
    location = request.form.get("location", "").strip()
    notes = request.form.get("notes", "").strip()
    if len(serial_number) > 150:
        raise ValueError("O número de série deve ter no máximo 150 caracteres.")
    if len(location) > 200:
        raise ValueError("A localização deve ter no máximo 200 caracteres.")
    if len(notes) > 5000:
        raise ValueError("As observações devem ter no máximo 5.000 caracteres.")
    return material_id, serial_number, location, notes


def process_load_photos(uploads):
    uploads = [upload for upload in uploads if upload and upload.filename]
    if len(uploads) > MAX_LOAD_PHOTOS:
        raise ValueError(f"Envie no máximo {MAX_LOAD_PHOTOS} fotos por carga.")
    return [process_material_photo(upload) for upload in uploads]


def load_entry_rows(db, query=""):
    sql = """SELECT le.*,m.description material_description,m.load_sheet material_fcg,
                    (SELECT COUNT(*) FROM load_entry_photos lp WHERE lp.load_entry_id=le.id) photo_count,
                    (SELECT thumbnail_data FROM load_entry_photos lp
                     WHERE lp.load_entry_id=le.id ORDER BY lp.id LIMIT 1) thumbnail_data
             FROM load_entries le JOIN materials m ON m.id=le.material_id"""
    params = ()
    if query:
        term = f"%{query.lower()}%"
        sql += """ WHERE LOWER(le.bmp) LIKE ? OR LOWER(m.description) LIKE ?
                   OR LOWER(le.serial_number) LIKE ? OR LOWER(le.location) LIKE ?"""
        params = (term, term, term, term)
    sql += " ORDER BY le.id DESC"
    return db.execute(sql, params).fetchall()


@bp.get("/materials")
@roles_allowed("manager", "staff", "infra")
def materials():
    db = get_db()
    query = request.args.get("q", "").strip()
    if query:
        term = f"%{query.lower()}%"
        rows = db.execute(
            """SELECT id,description,load_sheet,thumbnail_data,created_at
               FROM materials
               WHERE LOWER(description) LIKE ? OR LOWER(load_sheet) LIKE ?""",
            (term, term),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id,description,load_sheet,thumbnail_data,created_at FROM materials"
        ).fetchall()
    rows = sorted(rows, key=lambda material: alphabetical_key(material["description"]))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 20
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    visible = rows[(page - 1) * per_page:page * per_page]
    return render_template(
        "materials.html", materials=visible, total=len(rows), query=query,
        page=page, pages=pages,
    )


@bp.route("/materials/new", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "infra")
def new_material():
    if request.method == "POST":
        try:
            description, load_sheet, notes = material_form_values()
            processed = process_material_photo(request.files.get("photo"))
            photo, thumbnail = processed or ("", "")
            db = get_db()
            db.execute(
                """INSERT INTO materials
                   (description,load_sheet,notes,photo_data,thumbnail_data)
                   VALUES(?,?,?,?,?)""",
                (description, load_sheet, notes, photo, thumbnail),
            )
            db.commit()
            flash("Material cadastrado.", "success")
            return redirect(url_for("infra.materials"))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao cadastrar material: {exc}")
            flash("Erro interno ao cadastrar o material.", "danger")
    return render_template("material_form.html", material=None, form_title="Novo material")


@bp.get("/materials/<int:material_id>")
@roles_allowed("manager", "staff", "infra")
def material_detail(material_id):
    material = get_db().execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    if not material:
        flash("Material não encontrado.", "warning")
        return redirect(url_for("infra.materials"))
    return render_template("material_detail.html", material=material)


@bp.route("/materials/<int:material_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "infra")
def edit_material(material_id):
    db = get_db()
    material = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    if not material:
        flash("Material não encontrado.", "warning")
        return redirect(url_for("infra.materials"))
    if request.method == "POST":
        try:
            description, load_sheet, notes = material_form_values()
            processed = process_material_photo(request.files.get("photo"))
            if processed:
                photo, thumbnail = processed
            elif request.form.get("remove_photo") == "1":
                photo, thumbnail = "", ""
            else:
                photo, thumbnail = material["photo_data"], material["thumbnail_data"]
            db.execute(
                """UPDATE materials SET description=?,load_sheet=?,notes=?,photo_data=?,
                   thumbnail_data=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (description, load_sheet, notes, photo, thumbnail, material_id),
            )
            db.commit()
            flash("Material atualizado.", "success")
            return redirect(url_for("infra.material_detail", material_id=material_id))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao editar material {material_id}: {exc}")
            flash("Erro interno ao atualizar o material.", "danger")
        material = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    return render_template("material_form.html", material=material, form_title="Editar material")


@bp.post("/materials/<int:material_id>/delete")
@roles_allowed("manager", "staff", "infra")
def delete_material(material_id):
    db = get_db()
    try:
        if db.execute("SELECT 1 FROM load_entries WHERE material_id=? LIMIT 1", (material_id,)).fetchone():
            flash("Este material está vinculado a uma Relação de Carga e não pode ser apagado.", "danger")
            return redirect(url_for("infra.materials"))
        deleted = db.execute("DELETE FROM materials WHERE id=?", (material_id,))
        db.commit()
        flash(
            "Material apagado." if deleted.rowcount else "Material não encontrado.",
            "success" if deleted.rowcount else "warning",
        )
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao apagar material {material_id}: {exc}")
        flash("Erro interno ao apagar o material.", "danger")
    return redirect(url_for("infra.materials"))


@bp.get("/load-relation")
@roles_allowed("manager", "staff", "infra")
def load_relation():
    db = get_db()
    query = request.args.get("q", "").strip()
    rows = load_entry_rows(db, query)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 20
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    visible = rows[(page - 1) * per_page:page * per_page]
    return render_template(
        "load_relation.html", entries=visible, total=len(rows), query=query,
        page=page, pages=pages,
    )


@bp.route("/load-relation/new", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "infra")
def new_load_entry():
    db = get_db()
    materials = material_options(db)
    if request.method == "POST":
        try:
            material_id, serial_number, location, notes = load_form_values(db)
            photos = process_load_photos(request.files.getlist("photos"))
            with db:
                pending_bmp = f"pending-{uuid.uuid4().hex}"
                cursor = db.execute(
                    """INSERT INTO load_entries(material_id,bmp,serial_number,location,notes)
                       VALUES(?,?,?,?,?)""",
                    (material_id, pending_bmp, serial_number, location, notes),
                )
                entry_id = cursor.lastrowid
                bmp = f"BMP-{entry_id:06d}"
                db.execute("UPDATE load_entries SET bmp=? WHERE id=?", (bmp, entry_id))
                for photo, thumbnail in photos:
                    db.execute(
                        """INSERT INTO load_entry_photos(load_entry_id,photo_data,thumbnail_data)
                           VALUES(?,?,?)""",
                        (entry_id, photo, thumbnail),
                    )
            flash(f"Carga cadastrada com o código {bmp}.", "success")
            return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao cadastrar carga: {exc}")
            flash("Erro interno ao cadastrar a carga.", "danger")
    return render_template(
        "load_entry_form.html", entry=None, materials=materials,
        photos=[], form_title="Nova carga", max_photos=MAX_LOAD_PHOTOS,
    )


@bp.get("/load-relation/<int:entry_id>")
@roles_allowed("manager", "staff", "infra")
def load_entry_detail(entry_id):
    db = get_db()
    entry = db.execute(
        """SELECT le.*,m.description material_description,m.load_sheet material_fcg,
                  u.name discharged_by_name
           FROM load_entries le JOIN materials m ON m.id=le.material_id
           LEFT JOIN users u ON u.id=le.discharged_by WHERE le.id=?""",
        (entry_id,),
    ).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
        return redirect(url_for("infra.load_relation"))
    photos = db.execute(
        "SELECT * FROM load_entry_photos WHERE load_entry_id=? ORDER BY id", (entry_id,)
    ).fetchall()
    return render_template("load_entry_detail.html", entry=entry, photos=photos)


@bp.get("/load-relation/<int:entry_id>/qr-code")
@roles_allowed("manager", "staff", "infra")
def load_entry_qr_code(entry_id):
    entry = get_db().execute(
        """SELECT le.id,le.bmp,le.status,m.description material_description
           FROM load_entries le JOIN materials m ON m.id=le.material_id WHERE le.id=?""",
        (entry_id,),
    ).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
        return redirect(url_for("infra.load_relation"))
    detail_url = url_for("infra.load_entry_detail", entry_id=entry_id, _external=True)
    qr_image = generate_qrcode_base64(detail_url)
    return render_template(
        "load_entry_qr_code.html", entry=entry, detail_url=detail_url, qr_image=qr_image,
    )


@bp.post("/load-relation/<int:entry_id>/discharge")
@roles_allowed("manager", "staff", "infra")
def discharge_load_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT bmp,status FROM load_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
    elif entry["status"] == "discharged":
        flash(f"A carga {entry['bmp']} já foi descarregada.", "warning")
    else:
        db.execute(
            """UPDATE load_entries SET status='discharged',discharged_at=CURRENT_TIMESTAMP,
               discharged_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (g.user["id"], entry_id),
        )
        db.commit()
        flash(f"Carga {entry['bmp']} descarregada e mantida no histórico.", "success")
    return redirect(url_for("infra.load_relation"))


@bp.route("/load-relation/<int:entry_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "staff", "infra")
def edit_load_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
        return redirect(url_for("infra.load_relation"))
    photos = db.execute(
        "SELECT * FROM load_entry_photos WHERE load_entry_id=? ORDER BY id", (entry_id,)
    ).fetchall()
    if request.method == "POST":
        try:
            material_id, serial_number, location, notes = load_form_values(db)
            remove_ids = set()
            for value in request.form.getlist("remove_photo_ids"):
                try:
                    remove_ids.add(int(value))
                except ValueError:
                    raise ValueError("Seleção de foto inválida.")
            valid_ids = {photo["id"] for photo in photos}
            remove_ids &= valid_ids
            new_photos = process_load_photos(request.files.getlist("photos"))
            if len(photos) - len(remove_ids) + len(new_photos) > MAX_LOAD_PHOTOS:
                raise ValueError(f"Cada carga pode possuir no máximo {MAX_LOAD_PHOTOS} fotos.")
            with db:
                db.execute(
                    """UPDATE load_entries SET material_id=?,serial_number=?,location=?,notes=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (material_id, serial_number, location, notes, entry_id),
                )
                for photo_id in remove_ids:
                    db.execute(
                        "DELETE FROM load_entry_photos WHERE id=? AND load_entry_id=?",
                        (photo_id, entry_id),
                    )
                for photo, thumbnail in new_photos:
                    db.execute(
                        """INSERT INTO load_entry_photos(load_entry_id,photo_data,thumbnail_data)
                           VALUES(?,?,?)""",
                        (entry_id, photo, thumbnail),
                    )
            flash("Carga atualizada.", "success")
            return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao editar carga {entry_id}: {exc}")
            flash("Erro interno ao atualizar a carga.", "danger")
        entry = db.execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
        photos = db.execute(
            "SELECT * FROM load_entry_photos WHERE load_entry_id=? ORDER BY id", (entry_id,)
        ).fetchall()
    return render_template(
        "load_entry_form.html", entry=entry, materials=material_options(db),
        photos=photos, form_title="Editar carga", max_photos=MAX_LOAD_PHOTOS,
    )


@bp.post("/load-relation/<int:entry_id>/delete")
@roles_allowed("manager", "staff", "infra")
def delete_load_entry(entry_id):
    db = get_db()
    try:
        deleted = db.execute("DELETE FROM load_entries WHERE id=?", (entry_id,))
        db.commit()
        flash(
            "Carga apagada." if deleted.rowcount else "Carga não encontrada.",
            "success" if deleted.rowcount else "warning",
        )
    except Exception as exc:
        db.rollback()
        current_app.logger.error(f"Erro ao apagar carga {entry_id}: {exc}")
        flash("Erro interno ao apagar a carga.", "danger")
    return redirect(url_for("infra.load_relation"))


@bp.get("/load-relation/report.pdf")
@roles_allowed("manager", "staff", "infra")
def load_relation_report():
    query = request.args.get("q", "").strip()
    report = build_load_relation_pdf(load_entry_rows(get_db(), query), local_today(), query)
    return send_file(
        report,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"relacao-de-carga-{local_today().isoformat()}.pdf",
    )
