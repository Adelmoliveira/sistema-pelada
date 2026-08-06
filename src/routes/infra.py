import calendar
import uuid
from datetime import date, timedelta

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.load_relation_pdf import build_load_relation_pdf
from src.services.load_qr_labels_pdf import build_load_qr_labels_pdf
from src.services.material_photos import process_material_photo
from src.services.pix import generate_qrcode_base64
from src.utils import alphabetical_key, local_today


bp = Blueprint("infra", __name__, url_prefix="/infra")
MAX_LOAD_PHOTOS = 6
LOAD_AREAS = {
    "BAR": "Bar",
    "COZ": "Cozinha",
    "SAL": "Salão",
    "HIS": "Sala Histórica",
    "VES": "Vestiário",
    "BAN": "Banheiros",
}
LOAD_STATUS_LABELS = {
    "active": "Ativo",
    "maintenance": "Em manutenção",
    "discharged": "Baixado (Descarregado)",
    "lost": "Extraviado",
    "borrowed": "Emprestado",
}
LOAD_STATUS_CLASSES = {
    "active": "text-bg-success",
    "maintenance": "text-bg-warning",
    "discharged": "text-bg-secondary",
    "lost": "text-bg-danger",
    "borrowed": "text-bg-info",
}


def next_load_check_date(today=None):
    today = today or local_today()
    month_index = today.year * 12 + today.month - 1 + 6
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def bmp_code(entry_id, area_code):
    return f"BMP-{entry_id:06d} | {area_code}"


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
    area_code = request.form.get("area_code", "").strip().upper()
    if area_code not in LOAD_AREAS:
        raise ValueError("Selecione uma área válida.")
    serial_number = request.form.get("serial_number", "").strip()
    location = request.form.get("location", "").strip()
    responsible = request.form.get("responsible", "").strip()
    status = request.form.get("status", "active").strip().lower()
    if status not in LOAD_STATUS_LABELS:
        raise ValueError("Selecione uma situação válida.")
    notes = request.form.get("notes", "").strip()
    if len(serial_number) > 150:
        raise ValueError("O número de série deve ter no máximo 150 caracteres.")
    if len(location) > 200:
        raise ValueError("A localização deve ter no máximo 200 caracteres.")
    if len(responsible) > 200:
        raise ValueError("O responsável deve ter no máximo 200 caracteres.")
    if len(notes) > 5000:
        raise ValueError("As observações devem ter no máximo 5.000 caracteres.")
    return material_id, area_code, serial_number, location, responsible, status, notes


def process_load_photos(uploads):
    uploads = [upload for upload in uploads if upload and upload.filename]
    if len(uploads) > MAX_LOAD_PHOTOS:
        raise ValueError(f"Envie no máximo {MAX_LOAD_PHOTOS} fotos por carga.")
    return [process_material_photo(upload) for upload in uploads]


def load_entry_rows(db, query="", area_code="", status="", location="", responsible="", due=""):
    sql = """SELECT le.*,m.description material_description,m.load_sheet material_fcg,
                    (SELECT COUNT(*) FROM load_entry_photos lp WHERE lp.load_entry_id=le.id) photo_count,
                    (SELECT thumbnail_data FROM load_entry_photos lp
                     WHERE lp.load_entry_id=le.id ORDER BY lp.id LIMIT 1) thumbnail_data
             FROM load_entries le JOIN materials m ON m.id=le.material_id"""
    conditions = []
    params = []
    if query:
        term = f"%{query.lower()}%"
        conditions.append("(LOWER(le.bmp) LIKE ? OR LOWER(m.description) LIKE ? OR LOWER(le.serial_number) LIKE ? OR LOWER(le.location) LIKE ?)")
        params.extend((term, term, term, term))
    if area_code in LOAD_AREAS:
        conditions.append("le.area_code=?")
        params.append(area_code)
    if status in LOAD_STATUS_LABELS:
        conditions.append("le.status=?")
        params.append(status)
    if location:
        conditions.append("LOWER(le.location) LIKE ?")
        params.append(f"%{location.lower()}%")
    if responsible:
        conditions.append("LOWER(le.responsible) LIKE ?")
        params.append(f"%{responsible.lower()}%")
    if due == "pending":
        conditions.append("le.status NOT IN ('discharged','lost') AND (le.next_check_due_at IS NULL OR date(le.next_check_due_at)<=date('now'))")
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY le.id DESC"
    return db.execute(sql, tuple(params)).fetchall()


@bp.get("/materials")
@roles_allowed("manager", "infra")
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
@roles_allowed("manager", "infra")
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
@roles_allowed("manager", "infra")
def material_detail(material_id):
    material = get_db().execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    if not material:
        flash("Material não encontrado.", "warning")
        return redirect(url_for("infra.materials"))
    return render_template("material_detail.html", material=material)


@bp.route("/materials/<int:material_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "infra")
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
@roles_allowed("manager", "infra")
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
@roles_allowed("manager", "infra")
def load_relation():
    db = get_db()
    query = request.args.get("q", "").strip()
    area_code = request.args.get("area", "").strip().upper()
    status = request.args.get("status", "").strip().lower()
    location = request.args.get("location", "").strip()
    responsible = request.args.get("responsible", "").strip()
    due = request.args.get("due", "").strip()
    rows = load_entry_rows(db, query, area_code, status, location, responsible, due)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 20
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    visible = rows[(page - 1) * per_page:page * per_page]
    due_count = db.execute(
        """SELECT COUNT(*) total FROM load_entries
           WHERE status NOT IN ('discharged','lost')
             AND (next_check_due_at IS NULL OR date(next_check_due_at)<=?)""",
        (local_today().isoformat(),),
    ).fetchone()["total"]
    due_soon_count = db.execute(
        """SELECT COUNT(*) total FROM load_entries
           WHERE status NOT IN ('discharged','lost')
             AND next_check_due_at IS NOT NULL
             AND date(next_check_due_at)>?
             AND date(next_check_due_at)<=?""",
        (local_today().isoformat(), (local_today() + timedelta(days=30)).isoformat()),
    ).fetchone()["total"]
    return render_template(
        "load_relation.html", entries=visible, total=len(rows), query=query,
        page=page, pages=pages, area_code=area_code, load_areas=LOAD_AREAS, due_count=due_count,
        due_soon_count=due_soon_count,
        status=status, location=location, responsible=responsible, due=due,
        load_statuses=LOAD_STATUS_LABELS, load_status_classes=LOAD_STATUS_CLASSES,
    )


@bp.get("/load-relation/check")
@roles_allowed("manager", "infra")
def load_check():
    db = get_db()
    today = local_today().isoformat()
    entries = db.execute(
        """SELECT le.id,le.bmp,le.location,le.next_check_due_at,m.description material_description
           FROM load_entries le JOIN materials m ON m.id=le.material_id
           WHERE le.status NOT IN ('discharged','lost') AND (le.next_check_due_at IS NULL OR date(le.next_check_due_at)<=?)
           ORDER BY CASE WHEN le.next_check_due_at IS NULL THEN 0 ELSE 1 END,le.next_check_due_at,le.id""",
        (today,),
    ).fetchall()
    total = db.execute("SELECT COUNT(*) total FROM load_entries WHERE status NOT IN ('discharged','lost')").fetchone()["total"]
    return render_template("load_check.html", entries=entries, total=total, today=today)


@bp.get("/load-relation/qr-codes")
@roles_allowed("manager", "infra")
def load_qr_codes():
    area_code = request.args.get("area", "").strip().upper()
    entries = load_entry_rows(get_db(), area_code=area_code)
    return render_template(
        "load_qr_codes.html", entries=entries, area_code=area_code,
        load_areas=LOAD_AREAS,
    )


@bp.post("/load-relation/qr-codes.pdf")
@roles_allowed("manager", "infra")
def load_qr_codes_pdf():
    raw_ids = request.form.getlist("entry_ids")
    try:
        entry_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except ValueError:
        entry_ids = []
    if not entry_ids:
        flash("Selecione ao menos um BMP para gerar os códigos QR.", "danger")
        return redirect(url_for("infra.load_qr_codes", area=request.form.get("area_code", "")))
    if len(entry_ids) > 200:
        flash("Selecione no máximo 200 BMPs por impressão.", "danger")
        return redirect(url_for("infra.load_qr_codes", area=request.form.get("area_code", "")))
    placeholders = ",".join("?" for _ in entry_ids)
    entries = get_db().execute(
        f"""SELECT le.id,le.bmp,le.location,m.description material_description
            FROM load_entries le JOIN materials m ON m.id=le.material_id
            WHERE le.id IN ({placeholders}) ORDER BY le.area_code,le.id""",
        tuple(entry_ids),
    ).fetchall()
    size = request.form.get("size", "standard")
    if size not in ("small", "standard", "large"):
        size = "standard"
    report = build_load_qr_labels_pdf(entries, request.url_root, size)
    return send_file(
        report, mimetype="application/pdf", as_attachment=True,
        download_name=f"etiquetas-qr-bmp-{local_today().isoformat()}.pdf",
    )


@bp.route("/load-relation/new", methods=["GET", "POST"])
@roles_allowed("manager", "infra")
def new_load_entry():
    db = get_db()
    materials = material_options(db)
    if request.method == "POST":
        try:
            material_id, area_code, serial_number, location, responsible, status, notes = load_form_values(db)
            photos = process_load_photos(request.files.getlist("photos"))
            with db:
                pending_bmp = f"pending-{uuid.uuid4().hex}"
                cursor = db.execute(
                    """INSERT INTO load_entries(material_id,bmp,serial_number,location,responsible,status,notes)
                       VALUES(?,?,?,?,?,?,?)""",
                    (material_id, pending_bmp, serial_number, location, responsible, status, notes),
                )
                entry_id = cursor.lastrowid
                bmp = bmp_code(entry_id, area_code)
                db.execute("UPDATE load_entries SET bmp=?,area_code=? WHERE id=?", (bmp, area_code, entry_id))
                for photo, thumbnail in photos:
                    db.execute(
                        """INSERT INTO load_entry_photos
                           (load_entry_id,photo_data,thumbnail_data,photo_kind,captured_at,captured_by)
                           VALUES(?,?,?,'registration',CURRENT_TIMESTAMP,?)""",
                        (entry_id, photo, thumbnail, g.user["id"]),
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
        photos=[], form_title="Nova carga", max_photos=MAX_LOAD_PHOTOS, load_areas=LOAD_AREAS,
        load_statuses=LOAD_STATUS_LABELS,
    )


@bp.route("/load-relation/batch", methods=["GET", "POST"])
@roles_allowed("manager", "infra")
def batch_load_entries():
    db = get_db()
    materials = material_options(db)
    if request.method == "POST":
        try:
            material_id, area_code, serial_number, location, responsible, status, notes = load_form_values(db)
            quantity = int(request.form.get("quantity", "0"))
            if quantity < 1 or quantity > 500:
                raise ValueError("Informe uma quantidade entre 1 e 500 unidades.")
            prefix = request.form.get("serial_prefix", "").strip()
            if len(prefix) > 100:
                raise ValueError("O prefixo do número de série deve ter no máximo 100 caracteres.")
            generated = []
            with db:
                for index in range(1, quantity + 1):
                    pending_bmp = f"pending-{uuid.uuid4().hex}"
                    serial = f"{prefix}{index:03d}" if prefix else serial_number
                    cursor = db.execute(
                        """INSERT INTO load_entries(material_id,bmp,serial_number,location,responsible,status,notes)
                           VALUES(?,?,?,?,?,?,?)""",
                        (material_id, pending_bmp, serial, location, responsible, status, notes),
                    )
                    entry_id = cursor.lastrowid
                    bmp = bmp_code(entry_id, area_code)
                    db.execute("UPDATE load_entries SET bmp=?,area_code=? WHERE id=?", (bmp, area_code, entry_id))
                    generated.append({"id": entry_id, "bmp": bmp, "serial": serial})
            return render_template("load_batch_result.html", generated=generated, quantity=quantity)
        except (ValueError, TypeError) as exc:
            flash(str(exc), "danger")
        except Exception:
            db.rollback()
            current_app.logger.exception("Erro ao cadastrar cargas em lote")
            flash("Erro interno ao cadastrar as cargas.", "danger")
    return render_template("load_batch_form.html", materials=materials, load_areas=LOAD_AREAS,
                           load_statuses=LOAD_STATUS_LABELS)


@bp.get("/load-relation/<int:entry_id>")
@roles_allowed("manager", "infra")
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
        """SELECT lp.*,u.name captured_by_name FROM load_entry_photos lp
           LEFT JOIN users u ON u.id=lp.captured_by
           WHERE lp.load_entry_id=? ORDER BY lp.id""", (entry_id,)
    ).fetchall()
    movements = db.execute(
        """SELECT lm.*,u.name moved_by_name FROM load_entry_movements lm
           LEFT JOIN users u ON u.id=lm.moved_by WHERE lm.load_entry_id=? ORDER BY lm.moved_at DESC,lm.id DESC""",
        (entry_id,),
    ).fetchall()
    check_due = entry["status"] not in ("discharged", "lost") and (
        not entry["next_check_due_at"] or str(entry["next_check_due_at"])[:10] <= local_today().isoformat()
    )
    return render_template("load_entry_detail.html", entry=entry, photos=photos, movements=movements,
                           check_due=check_due, load_statuses=LOAD_STATUS_LABELS,
                           load_status_classes=LOAD_STATUS_CLASSES)


@bp.post("/load-relation/<int:entry_id>/move")
@roles_allowed("manager", "infra")
def move_load_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT id,location,responsible,status FROM load_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
        return redirect(url_for("infra.load_relation"))
    to_location = request.form.get("location", "").strip()
    to_responsible = request.form.get("responsible", "").strip()
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Informe o motivo da movimentação.", "danger")
        return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))
    try:
        with db:
            db.execute("""INSERT INTO load_entry_movements
                (load_entry_id,from_location,to_location,from_responsible,to_responsible,reason,moved_by)
                VALUES(?,?,?,?,?,?,?)""", (entry_id, entry["location"] or "", to_location,
                entry["responsible"] or "", to_responsible, reason, g.user["id"]))
            db.execute("UPDATE load_entries SET location=?,responsible=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (to_location, to_responsible, entry_id))
        flash("Movimentação patrimonial registrada.", "success")
    except Exception:
        db.rollback()
        current_app.logger.exception("Erro ao movimentar carga %s", entry_id)
        flash("Erro interno ao registrar a movimentação.", "danger")
    return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))


@bp.post("/load-relation/<int:entry_id>/status")
@roles_allowed("manager", "infra")
def update_load_status(entry_id):
    status = request.form.get("status", "").strip().lower()
    if status not in LOAD_STATUS_LABELS:
        flash("Situação inválida.", "danger")
        return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))
    db = get_db()
    try:
        if status == "discharged":
            updated = db.execute(
                """UPDATE load_entries SET status=?,discharged_at=CURRENT_TIMESTAMP,
                   discharged_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, g.user["id"], entry_id),
            )
        else:
            updated = db.execute(
                """UPDATE load_entries SET status=?,discharged_at=NULL,discharged_by=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, entry_id),
            )
        db.commit()
        flash("Situação atualizada." if updated.rowcount else "Carga não encontrada.",
              "success" if updated.rowcount else "warning")
    except Exception:
        db.rollback()
        current_app.logger.exception("Erro ao alterar situação da carga %s", entry_id)
        flash("Erro interno ao atualizar a situação.", "danger")
    return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))


@bp.post("/load-relation/<int:entry_id>/check")
@roles_allowed("manager", "infra")
def check_load_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT id,bmp,status FROM load_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
    elif entry["status"] in ("discharged", "lost"):
        flash(f"A carga {entry['bmp']} está baixada/extraviada e não pode ser conferida.", "warning")
    else:
        db.execute(
            """UPDATE load_entries SET last_checked_at=CURRENT_TIMESTAMP,last_checked_by=?,
               next_check_due_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (g.user["id"], next_load_check_date(), entry_id),
        )
        db.commit()
        flash(f"Conferência da carga {entry['bmp']} registrada. Próxima conferência em 6 meses.", "success")
    return redirect(url_for("infra.load_entry_detail", entry_id=entry_id))


@bp.post("/load-relation/<int:entry_id>/check-auto")
@roles_allowed("manager", "infra")
def check_load_entry_auto(entry_id):
    """Atomically store QR conference evidence and register the conference."""
    db = get_db()
    try:
        entry = db.execute(
            "SELECT id,bmp,status FROM load_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not entry:
            return jsonify(ok=False, error="Carga não encontrada."), 404
        if entry["status"] in ("discharged", "lost"):
            return jsonify(
                ok=False,
                inconsistency=True,
                bmp=entry["bmp"],
                error=f"A carga {entry['bmp']} está baixada/extraviada e não pode ser conferida.",
            ), 409
        upload = request.files.get("photo")
        if not upload or not upload.filename:
            return jsonify(ok=False, error="Tire uma foto da carga para concluir a conferência."), 400
        photo, thumbnail = process_material_photo(upload)
        due_at = next_load_check_date()
        with db:
            db.execute(
                """INSERT INTO load_entry_photos
                   (load_entry_id,photo_data,thumbnail_data,photo_kind,captured_at,captured_by)
                   VALUES(?,?,?,'conference',CURRENT_TIMESTAMP,?)""",
                (entry_id, photo, thumbnail, g.user["id"]),
            )
            db.execute(
                """UPDATE load_entries SET last_checked_at=CURRENT_TIMESTAMP,
                   last_checked_by=?,next_check_due_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (g.user["id"], due_at, entry_id),
            )
        return jsonify(ok=True, bmp=entry["bmp"], next_check_due_at=due_at)
    except ValueError as exc:
        db.rollback()
        return jsonify(ok=False, error=str(exc)), 400
    except Exception:
        db.rollback()
        current_app.logger.exception("Erro na conferência automática da carga %s", entry_id)
        return jsonify(ok=False, error="Não foi possível registrar a conferência."), 500


@bp.get("/load-relation/<int:entry_id>/qr-code")
@roles_allowed("manager", "infra")
def load_entry_qr_code(entry_id):
    entry = get_db().execute(
        """SELECT le.id,le.bmp,le.area_code,le.status,m.description material_description
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
@roles_allowed("manager", "infra")
def discharge_load_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT bmp,status FROM load_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
    elif entry["status"] in ("discharged", "lost"):
        flash(f"A carga {entry['bmp']} já não está ativa.", "warning")
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
@roles_allowed("manager", "infra")
def edit_load_entry(entry_id):
    db = get_db()
    entry = db.execute("SELECT * FROM load_entries WHERE id=?", (entry_id,)).fetchone()
    if not entry:
        flash("Carga não encontrada.", "warning")
        return redirect(url_for("infra.load_relation"))
    photos = db.execute(
        """SELECT * FROM load_entry_photos
           WHERE load_entry_id=? AND photo_kind='registration' ORDER BY id""", (entry_id,)
    ).fetchall()
    if request.method == "POST":
        try:
            material_id, area_code, serial_number, location, responsible, status, notes = load_form_values(db)
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
                    """UPDATE load_entries SET material_id=?,area_code=?,bmp=?,serial_number=?,location=?,responsible=?,status=?,notes=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (material_id, area_code, bmp_code(entry_id, area_code), serial_number, location, responsible, status, notes, entry_id),
                )
                for photo_id in remove_ids:
                    db.execute(
                        """DELETE FROM load_entry_photos
                           WHERE id=? AND load_entry_id=? AND photo_kind='registration'""",
                        (photo_id, entry_id),
                    )
                for photo, thumbnail in new_photos:
                    db.execute(
                        """INSERT INTO load_entry_photos
                           (load_entry_id,photo_data,thumbnail_data,photo_kind,captured_at,captured_by)
                           VALUES(?,?,?,'registration',CURRENT_TIMESTAMP,?)""",
                        (entry_id, photo, thumbnail, g.user["id"]),
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
            """SELECT * FROM load_entry_photos
               WHERE load_entry_id=? AND photo_kind='registration' ORDER BY id""", (entry_id,)
        ).fetchall()
    return render_template(
        "load_entry_form.html", entry=entry, materials=material_options(db),
        photos=photos, form_title="Editar carga", max_photos=MAX_LOAD_PHOTOS,
        load_areas=LOAD_AREAS, load_statuses=LOAD_STATUS_LABELS,
    )


@bp.post("/load-relation/<int:entry_id>/delete")
@roles_allowed("manager", "infra")
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
@roles_allowed("manager", "infra")
def load_relation_report():
    query = request.args.get("q", "").strip()
    area_code = request.args.get("area", "").strip().upper()
    status = request.args.get("status", "").strip().lower()
    location = request.args.get("location", "").strip()
    responsible = request.args.get("responsible", "").strip()
    due = request.args.get("due", "").strip()
    report_filter = " · ".join(value for value in (query, area_code, LOAD_STATUS_LABELS.get(status, ""), location, responsible, due) if value)
    report = build_load_relation_pdf(
        load_entry_rows(get_db(), query, area_code, status, location, responsible, due), local_today(), report_filter,
    )
    return send_file(
        report,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"relacao-de-carga-{local_today().isoformat()}.pdf",
    )


@bp.get("/load-relation/report")
@roles_allowed("manager", "infra")
def load_relation_report_page():
    query = request.args.get("q", "").strip()
    area_code = request.args.get("area", "").strip().upper()
    status = request.args.get("status", "").strip().lower()
    location = request.args.get("location", "").strip()
    responsible = request.args.get("responsible", "").strip()
    due = request.args.get("due", "").strip()
    entries = load_entry_rows(get_db(), query, area_code, status, location, responsible, due)
    return render_template("load_report.html", entries=entries, total=len(entries), query=query,
                           area_code=area_code, status=status, location=location, responsible=responsible,
                           due=due, load_areas=LOAD_AREAS, load_statuses=LOAD_STATUS_LABELS,
                           load_status_classes=LOAD_STATUS_CLASSES)
