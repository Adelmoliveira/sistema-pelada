import calendar
import uuid
from datetime import date, timedelta

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.load_relation_pdf import build_load_relation_pdf
from src.services.load_quantity_pdf import build_load_quantity_pdf
from src.services.load_qr_labels_pdf import build_load_qr_labels_pdf
from src.services.material_photos import process_material_photo
from src.services.pix import generate_qrcode_base64
from src.utils import alphabetical_key, local_today


bp = Blueprint("infra", __name__, url_prefix="/infra")
MAX_LOAD_PHOTOS = 6
MAX_LOAD_REQUEST_BYTES = 4 * 1024 * 1024
LOAD_AREAS = {
    "BAR": "Bar",
    "COZ": "Cozinha",
    "SAL": "Salão",
    "HIS": "Sala Histórica",
    "VES": "Vestiário",
    "BAN": "Banheiros",
    "INT": "Interno sede",
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
LOAN_STATUS_LABELS = {"open": "Emprestado", "partial": "Devolução parcial", "returned": "Devolvido", "cancelled": "Cancelado"}
LOAN_STATUS_CLASSES = {"open": "text-bg-primary", "partial": "text-bg-warning", "returned": "text-bg-success", "cancelled": "text-bg-secondary"}


def next_load_check_date(today=None):
    today = today or local_today()
    month_index = today.year * 12 + today.month - 1 + 6
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def load_conference_filter(due, today=None, alias="le"):
    """Return the existing load-conference rule as SQL and parameters.

    Keeping these predicates in one place prevents the relation filters and
    the current-operation counter from disagreeing about a valid conference.
    """
    resolved_today = today or local_today()
    today = resolved_today.isoformat() if isinstance(resolved_today, date) else str(resolved_today)
    prefix = f"{alias}."
    active = f"{prefix}status NOT IN ('discharged','lost')"
    if due == "valid":
        return (
            f"{active} AND {prefix}last_checked_at IS NOT NULL "
            f"AND {prefix}next_check_due_at IS NOT NULL "
            f"AND date({prefix}next_check_due_at)>?",
            [today],
        )
    if due == "missing":
        return (
            f"{active} AND ({prefix}last_checked_at IS NULL "
            f"OR {prefix}next_check_due_at IS NULL)",
            [],
        )
    if due == "expired":
        return (
            f"{active} AND {prefix}last_checked_at IS NOT NULL "
            f"AND {prefix}next_check_due_at IS NOT NULL "
            f"AND date({prefix}next_check_due_at)<=?",
            [today],
        )
    if due == "pending":
        # Backward compatibility for existing links: this is the exact
        # previous combined "missing or expired" predicate.
        return (
            f"{active} AND ({prefix}next_check_due_at IS NULL "
            f"OR date({prefix}next_check_due_at)<=?)",
            [today],
        )
    return "", []


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


def loan_material_options(db):
    rows = db.execute(
        """SELECT m.id,m.description,m.load_sheet,
                  (SELECT COUNT(*) FROM load_entries le WHERE le.material_id=m.id AND le.status='active') active_quantity,
                  COALESCE((SELECT SUM(li.quantity-li.returned_quantity) FROM load_loan_items li
                            JOIN load_loans l ON l.id=li.loan_id
                            WHERE li.material_id=m.id AND l.status IN ('open','partial')),0) loaned_quantity
           FROM materials m ORDER BY m.description"""
    ).fetchall()
    return [dict(row, available_quantity=max(0, int(row["active_quantity"] or 0) - int(row["loaned_quantity"] or 0))) for row in rows]


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


def limit_load_upload_request():
    request.max_content_length = MAX_LOAD_REQUEST_BYTES


def load_entry_rows(db, query="", area_code="", status="", location="", responsible="", due="", material_id=None):
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
    if material_id:
        conditions.append("le.material_id=?")
        params.append(material_id)
    if status in LOAD_STATUS_LABELS:
        conditions.append("le.status=?")
        params.append(status)
    if location:
        conditions.append("LOWER(le.location) LIKE ?")
        params.append(f"%{location.lower()}%")
    if responsible:
        conditions.append("LOWER(le.responsible) LIKE ?")
        params.append(f"%{responsible.lower()}%")
    conference_condition, conference_params = load_conference_filter(due, alias="le")
    if conference_condition:
        conditions.append(conference_condition)
        params.extend(conference_params)
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
        except RequestEntityTooLarge:
            raise
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
@roles_allowed("manager", "infra", "staff")
def load_relation():
    db = get_db()
    query = request.args.get("q", "").strip()
    area_code = request.args.get("area", "").strip().upper()
    status = request.args.get("status", "").strip().lower()
    location = request.args.get("location", "").strip()
    responsible = request.args.get("responsible", "").strip()
    due = request.args.get("due", "").strip()
    try:
        material_id = int(request.args.get("material_id", ""))
    except (TypeError, ValueError):
        material_id = None
    rows = load_entry_rows(db, query, area_code, status, location, responsible, due, material_id)
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
    available_material_ids = {row["material_id"] for row in rows}
    available_materials = [
        material for material in material_options(db)
        if material["id"] in available_material_ids or material["id"] == material_id
    ]
    return render_template(
        "load_relation.html", entries=visible, total=len(rows), query=query,
        page=page, pages=pages, area_code=area_code, load_areas=LOAD_AREAS, due_count=due_count,
        due_soon_count=due_soon_count,
        status=status, location=location, responsible=responsible, due=due,
        material_id=material_id, materials=available_materials,
        load_statuses=LOAD_STATUS_LABELS, load_status_classes=LOAD_STATUS_CLASSES,
        all_entry_ids=[row["id"] for row in rows],
    )


@bp.post("/load-relation/bulk-edit")
@roles_allowed("manager", "infra")
def bulk_edit_load_entries():
    """Apply the same selected changes to several load entries at once."""
    db = get_db()
    raw_ids = request.form.getlist("entry_ids")
    try:
        entry_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        entry_ids = []

    redirect_args = {
        key: request.form.get(key, "")
        for key in ("q", "area", "status", "material_id", "location", "responsible", "due", "page")
        if request.form.get(key, "")
    }
    if not entry_ids:
        flash("Selecione ao menos uma carga para editar.", "warning")
        return redirect(url_for("infra.load_relation", **redirect_args))
    if len(entry_ids) > 5000:
        flash("Selecione no máximo 5.000 cargas por atualização.", "danger")
        return redirect(url_for("infra.load_relation", **redirect_args))

    updates = {}
    if request.form.get("apply_location") == "1":
        location = request.form.get("bulk_location", "").strip()
        if len(location) > 200:
            flash("A localização deve ter no máximo 200 caracteres.", "danger")
            return redirect(url_for("infra.load_relation", **redirect_args))
        updates["location"] = location
    if request.form.get("apply_area") == "1":
        area_code = request.form.get("bulk_area", "").strip().upper()
        if area_code not in LOAD_AREAS:
            flash("Selecione uma área válida.", "danger")
            return redirect(url_for("infra.load_relation", **redirect_args))
        updates["area_code"] = area_code
    if request.form.get("apply_responsible") == "1":
        responsible = request.form.get("bulk_responsible", "").strip()
        if len(responsible) > 200:
            flash("O responsável deve ter no máximo 200 caracteres.", "danger")
            return redirect(url_for("infra.load_relation", **redirect_args))
        updates["responsible"] = responsible
    status = request.form.get("bulk_status", "").strip().lower()
    if request.form.get("apply_status") == "1":
        if status not in LOAD_STATUS_LABELS:
            flash("Selecione uma situação válida.", "danger")
            return redirect(url_for("infra.load_relation", **redirect_args))
        updates["status"] = status
    if request.form.get("apply_notes") == "1":
        notes = request.form.get("bulk_notes", "").strip()
        if len(notes) > 5000:
            flash("As observações devem ter no máximo 5.000 caracteres.", "danger")
            return redirect(url_for("infra.load_relation", **redirect_args))
        updates["notes"] = notes

    if not updates:
        flash("Marque ao menos um campo para aplicar em lote.", "warning")
        return redirect(url_for("infra.load_relation", **redirect_args))

    placeholders = ",".join("?" for _ in entry_ids)
    try:
        entries = db.execute(
            f"SELECT id,area_code,location,responsible,status FROM load_entries WHERE id IN ({placeholders})",
            tuple(entry_ids),
        ).fetchall()
        if len(entries) != len(entry_ids):
            flash("Uma ou mais cargas selecionadas não foram encontradas.", "danger")
            return redirect(url_for("infra.load_relation", **redirect_args))
        with db:
            for entry in entries:
                movement_changed = any(
                    field in updates and (entry[field] or "") != updates[field]
                    for field in ("area_code", "location", "responsible")
                )
                if movement_changed:
                    movement_reason = "Atualização em lote"
                    if "area_code" in updates and (entry["area_code"] or "") != updates["area_code"]:
                        movement_reason += " (área)"
                    db.execute(
                        """INSERT INTO load_entry_movements
                           (load_entry_id,from_location,to_location,from_responsible,to_responsible,reason,moved_by)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            entry["id"], entry["location"] or "", updates.get("location", entry["location"] or ""),
                            entry["responsible"] or "", updates.get("responsible", entry["responsible"] or ""),
                            movement_reason, g.user["id"],
                        ),
                    )
                set_parts = [f"{field}=?" for field in updates]
                values = [updates[field] for field in updates]
                # O código BMP incorpora a área (por exemplo, ``BMP-000001 | COZ``).
                # Ao alterar a área em lote, atualize-o junto com ``area_code``;
                # caso contrário a alteração fica gravada no banco, mas a tela
                # continua exibindo o BMP antigo e parece que nada mudou.
                if "area_code" in updates:
                    set_parts.append("bmp=?")
                    values.append(bmp_code(entry["id"], updates["area_code"]))
                if updates.get("status") == "discharged":
                    set_parts.extend(["discharged_at=CURRENT_TIMESTAMP", "discharged_by=?"])
                    values.append(g.user["id"])
                elif "status" in updates:
                    set_parts.extend(["discharged_at=NULL", "discharged_by=NULL"])
                set_parts.append("updated_at=CURRENT_TIMESTAMP")
                values.append(entry["id"])
                db.execute(
                    f"UPDATE load_entries SET {','.join(set_parts)} WHERE id=?",
                    tuple(values),
                )
            # Não mostre sucesso se a atualização não persistiu. Isso também
            # protege contra diferenças de schema/driver (por exemplo, a área
            # foi alterada, mas o BMP exibido continuou antigo).
            check_rows = db.execute(
                f"SELECT id,area_code,location,responsible,status,bmp FROM load_entries WHERE id IN ({placeholders})",
                tuple(entry_ids),
            ).fetchall()
            check_by_id = {row["id"]: row for row in check_rows}
            for entry_id in entry_ids:
                row = check_by_id.get(entry_id)
                if not row:
                    raise ValueError(f"A carga {entry_id} não foi encontrada após a atualização.")
                for field, expected in updates.items():
                    if (row[field] or "") != expected:
                        raise ValueError(f"Não foi possível atualizar a carga {entry_id}.")
                if "area_code" in updates and row["bmp"] != bmp_code(entry_id, updates["area_code"]):
                    raise ValueError(f"Não foi possível atualizar o BMP da carga {entry_id}.")
        flash(f"{len(entries)} carga(s) atualizada(s) com sucesso.", "success")
    except Exception:
        db.rollback()
        current_app.logger.exception("Erro ao editar cargas em lote: ids=%s", entry_ids)
        flash("Erro interno ao atualizar as cargas selecionadas.", "danger")
    return redirect(url_for("infra.load_relation", **redirect_args))


@bp.get("/load-relation/check")
@roles_allowed("manager", "infra", "staff")
def load_check():
    db = get_db()
    today = local_today().isoformat()
    pending_total = db.execute(
        """SELECT COUNT(*) total FROM load_entries
           WHERE status NOT IN ('discharged','lost')
             AND (next_check_due_at IS NULL OR date(next_check_due_at)<=?)""",
        (today,),
    ).fetchone()["total"]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 15
    pages = max(1, (pending_total + per_page - 1) // per_page)
    page = min(page, pages)
    entries = db.execute(
        """SELECT le.id,le.bmp,le.location,le.next_check_due_at,m.description material_description
           FROM load_entries le JOIN materials m ON m.id=le.material_id
           WHERE le.status NOT IN ('discharged','lost') AND (le.next_check_due_at IS NULL OR date(le.next_check_due_at)<=?)
           ORDER BY CASE WHEN le.next_check_due_at IS NULL THEN 0 ELSE 1 END,le.next_check_due_at,le.id
           LIMIT ? OFFSET ?""",
        (today, per_page, (page - 1) * per_page),
    ).fetchall()
    total = db.execute("SELECT COUNT(*) total FROM load_entries WHERE status NOT IN ('discharged','lost')").fetchone()["total"]
    return render_template(
        "load_check.html", entries=entries, total=total, pending_total=pending_total,
        page=page, pages=pages, today=today,
    )


@bp.get("/load-relation/qr-codes")
@roles_allowed("manager", "infra")
def load_qr_codes():
    area_code = request.args.get("area", "").strip().upper()
    try:
        material_id = int(request.args.get("material_id", ""))
    except (TypeError, ValueError):
        material_id = None
    db = get_db()
    entries = load_entry_rows(db, area_code=area_code, material_id=material_id)
    available_material_ids = {row["material_id"] for row in entries}
    available_materials = [material for material in material_options(db) if material["id"] in available_material_ids or material["id"] == material_id]
    return render_template(
        "load_qr_codes.html", entries=entries, area_code=area_code,
        load_areas=LOAD_AREAS, material_id=material_id, materials=available_materials,
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
        return redirect(url_for("infra.load_qr_codes", area=request.form.get("area_code", ""), material_id=request.form.get("material_id", "")))
    if len(entry_ids) > 200:
        flash("Selecione no máximo 200 BMPs por impressão.", "danger")
        return redirect(url_for("infra.load_qr_codes", area=request.form.get("area_code", ""), material_id=request.form.get("material_id", "")))
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
    if request.method == "POST":
        limit_load_upload_request()
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
        except RequestEntityTooLarge:
            raise
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
    photo_list = [dict(photo) for photo in photos]
    conference_timeline = []
    for index, photo in enumerate(photo_list):
        if photo.get("photo_kind") in ("reference", "conference"):
            conference_timeline.append({
                "current": photo,
                "previous": photo_list[index - 1] if index else None,
                "is_reference": photo.get("photo_kind") == "reference",
            })
    conference_timeline.reverse()
    check_due = entry["status"] not in ("discharged", "lost") and (
        not entry["next_check_due_at"] or str(entry["next_check_due_at"])[:10] <= local_today().isoformat()
    )
    return render_template("load_entry_detail.html", entry=entry, photos=photos, movements=movements,
                           conference_timeline=conference_timeline,
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
@roles_allowed("manager", "infra", "staff")
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
@roles_allowed("manager", "infra", "staff")
def check_load_entry_auto(entry_id):
    """Atomically store QR conference evidence and register the conference."""
    limit_load_upload_request()
    db = get_db()
    try:
        try:
            operation_entry_ids = [
                int(value) for value in request.form.getlist("operation_entry_ids")
            ]
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Operação de conferência inválida."), 400
        operation_entry_ids = list(dict.fromkeys([*operation_entry_ids, entry_id]))
        if len(operation_entry_ids) > 5000:
            return jsonify(ok=False, error="Operação de conferência muito extensa."), 400
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
            has_existing_photo = db.execute(
                "SELECT 1 FROM load_entry_photos WHERE load_entry_id=? LIMIT 1", (entry_id,)
            ).fetchone()
            photo_kind = "conference" if has_existing_photo else "reference"
            db.execute(
                """INSERT INTO load_entry_photos
                   (load_entry_id,photo_data,thumbnail_data,photo_kind,captured_at,captured_by)
                   VALUES(?,?,?,?,CURRENT_TIMESTAMP,?)""",
                (entry_id, photo, thumbnail, photo_kind, g.user["id"]),
            )
            db.execute(
                """UPDATE load_entries SET last_checked_at=CURRENT_TIMESTAMP,
                   last_checked_by=?,next_check_due_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (g.user["id"], due_at, entry_id),
            )
        placeholders = ",".join("?" for _ in operation_entry_ids)
        valid_condition, valid_params = load_conference_filter("valid", alias="le")
        checked_count = db.execute(
            f"""SELECT COUNT(DISTINCT le.id) total FROM load_entries le
                WHERE le.id IN ({placeholders}) AND le.last_checked_by=?
                  AND {valid_condition}
                  AND EXISTS (
                      SELECT 1 FROM load_entry_photos lp
                      WHERE lp.load_entry_id=le.id AND lp.captured_by=?
                        AND lp.photo_kind IN ('reference','conference')
                  )""",
            (*operation_entry_ids, g.user["id"], *valid_params, g.user["id"]),
        ).fetchone()["total"]
        return jsonify(
            ok=True, bmp=entry["bmp"], next_check_due_at=due_at,
            checked_count=int(checked_count or 0),
        )
    except ValueError as exc:
        db.rollback()
        return jsonify(ok=False, error=str(exc)), 400
    except RequestEntityTooLarge:
        db.rollback()
        raise
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
    if request.method == "POST":
        limit_load_upload_request()
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
        except RequestEntityTooLarge:
            raise
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


@bp.route("/loans", methods=["GET", "POST"])
@roles_allowed("manager", "infra")
def loans():
    if request.method == "POST":
        limit_load_upload_request()
    db = get_db()
    if request.method == "POST":
        try:
            borrower_name = request.form.get("borrower_name", "").strip()
            borrower_phone = request.form.get("borrower_phone", "").strip()
            borrower_document = request.form.get("borrower_document", "").strip()
            checkout_on = request.form.get("checkout_on", "").strip()
            due_on = request.form.get("due_on", "").strip()
            notes = request.form.get("notes", "").strip()
            if not borrower_name:
                raise ValueError("Informe quem está retirando os materiais.")
            if len(borrower_name) > 200 or len(borrower_phone) > 50 or len(borrower_document) > 100:
                raise ValueError("Os dados do responsável excedem o tamanho permitido.")
            try:
                checkout_date, due_date = date.fromisoformat(checkout_on), date.fromisoformat(due_on)
            except ValueError as exc:
                raise ValueError("Informe as datas de retirada e devolução.") from exc
            if due_date < checkout_date:
                raise ValueError("A devolução não pode ser anterior à retirada.")
            requested = {}
            for material_value, quantity_value in zip(request.form.getlist("material_id"), request.form.getlist("quantity")):
                if not material_value and not quantity_value:
                    continue
                try:
                    material_id, quantity = int(material_value), int(quantity_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Selecione o material e informe uma quantidade válida.") from exc
                if quantity <= 0:
                    raise ValueError("A quantidade emprestada deve ser maior que zero.")
                requested[material_id] = requested.get(material_id, 0) + quantity
            if not requested:
                raise ValueError("Adicione pelo menos um material ao empréstimo.")
            available = {item["id"]: item for item in loan_material_options(db)}
            for material_id, quantity in requested.items():
                material = available.get(material_id)
                if not material:
                    raise ValueError("Material não encontrado.")
                if quantity > material["available_quantity"]:
                    raise ValueError(f"{material['description']}: somente {material['available_quantity']} unidade(s) disponível(is).")
            departure_photo = departure_thumbnail = ""
            upload = request.files.get("departure_photo")
            if upload and upload.filename:
                departure_photo, departure_thumbnail = process_material_photo(upload)
            with db:
                cursor = db.execute(
                    """INSERT INTO load_loans
                       (borrower_name,borrower_phone,borrower_document,checkout_on,due_on,notes,
                        departure_photo_data,departure_thumbnail_data,created_by)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (borrower_name, borrower_phone, borrower_document, checkout_on, due_on, notes,
                     departure_photo, departure_thumbnail, g.user["id"]),
                )
                loan_id = cursor.lastrowid
                descriptions = []
                for material_id, quantity in requested.items():
                    db.execute("INSERT INTO load_loan_items(loan_id,material_id,quantity) VALUES(?,?,?)", (loan_id, material_id, quantity))
                    descriptions.append(f"{quantity}x {available[material_id]['description']}")
                db.execute(
                    "INSERT INTO load_loan_history(loan_id,event_type,description,changed_by) VALUES(?,'checkout',?,?)",
                    (loan_id, "Retirada: " + ", ".join(descriptions), g.user["id"]),
                )
            flash(f"Empréstimo EMP-{loan_id:06d} registrado.", "success")
            return redirect(url_for("infra.loan_detail", loan_id=loan_id))
        except ValueError as exc:
            db.rollback()
            flash(str(exc), "danger")
        except RequestEntityTooLarge:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            current_app.logger.exception("Erro ao registrar empréstimo de carga")
            flash("Não foi possível registrar o empréstimo.", "danger")
    status = request.args.get("status", "open").strip()
    query = request.args.get("q", "").strip()
    conditions, params = [], []
    if status in LOAN_STATUS_LABELS:
        conditions.append("l.status=?")
        params.append(status)
    elif status == "overdue":
        conditions.extend(["l.status IN ('open','partial')", "l.due_on<?"])
        params.append(local_today().isoformat())
    if query:
        conditions.append("(LOWER(l.borrower_name) LIKE ? OR LOWER(l.borrower_document) LIKE ?)")
        params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.execute(
        f"""SELECT l.*,u.name created_by_name,
                   (SELECT SUM(quantity) FROM load_loan_items WHERE loan_id=l.id) total_quantity,
                   (SELECT SUM(returned_quantity) FROM load_loan_items WHERE loan_id=l.id) returned_quantity
            FROM load_loans l LEFT JOIN users u ON u.id=l.created_by{where}
            ORDER BY CASE WHEN l.status IN ('open','partial') THEN 0 ELSE 1 END,l.due_on,l.id DESC""",
        tuple(params),
    ).fetchall()
    return render_template("load_loans.html", loans=rows, materials=loan_material_options(db), today=local_today(),
                           status=status, query=query, loan_statuses=LOAN_STATUS_LABELS,
                           loan_status_classes=LOAN_STATUS_CLASSES)


@bp.get("/loans/<int:loan_id>")
@roles_allowed("manager", "infra")
def loan_detail(loan_id):
    db = get_db()
    loan = db.execute(
        """SELECT l.*,u.name created_by_name,ru.name returned_by_name FROM load_loans l
           LEFT JOIN users u ON u.id=l.created_by LEFT JOIN users ru ON ru.id=l.returned_by WHERE l.id=?""",
        (loan_id,),
    ).fetchone()
    if not loan:
        flash("Empréstimo não encontrado.", "warning")
        return redirect(url_for("infra.loans"))
    items = db.execute(
        """SELECT li.*,m.description,m.load_sheet FROM load_loan_items li
           JOIN materials m ON m.id=li.material_id WHERE li.loan_id=? ORDER BY m.description""",
        (loan_id,),
    ).fetchall()
    history = db.execute(
        """SELECT h.*,u.name changed_by_name FROM load_loan_history h
           LEFT JOIN users u ON u.id=h.changed_by WHERE h.loan_id=? ORDER BY h.id DESC""",
        (loan_id,),
    ).fetchall()
    return render_template("load_loan_detail.html", loan=loan, items=items, history=history, today=local_today(),
                           loan_statuses=LOAN_STATUS_LABELS, loan_status_classes=LOAN_STATUS_CLASSES)


@bp.post("/loans/<int:loan_id>/return")
@roles_allowed("manager", "infra")
def return_loan(loan_id):
    limit_load_upload_request()
    db = get_db()
    loan = db.execute("SELECT * FROM load_loans WHERE id=?", (loan_id,)).fetchone()
    if not loan or loan["status"] not in ("open", "partial"):
        flash("Este empréstimo não aceita devoluções.", "warning")
        return redirect(url_for("infra.loan_detail", loan_id=loan_id))
    try:
        items = db.execute("SELECT * FROM load_loan_items WHERE loan_id=?", (loan_id,)).fetchall()
        returns, descriptions = {}, []
        for item in items:
            value = request.form.get(f"return_{item['id']}", "0").strip() or "0"
            try:
                quantity = int(value)
            except ValueError as exc:
                raise ValueError("Informe quantidades de devolução válidas.") from exc
            remaining = int(item["quantity"]) - int(item["returned_quantity"])
            if quantity < 0 or quantity > remaining:
                raise ValueError("A devolução não pode ultrapassar a quantidade pendente.")
            if quantity:
                returns[item["id"]] = quantity
                descriptions.append(f"{quantity} unidade(s) do item #{item['id']}")
        if not returns:
            raise ValueError("Informe ao menos uma quantidade devolvida.")
        return_photo = return_thumbnail = ""
        upload = request.files.get("return_photo")
        if upload and upload.filename:
            return_photo, return_thumbnail = process_material_photo(upload)
        with db:
            for item_id, quantity in returns.items():
                db.execute("UPDATE load_loan_items SET returned_quantity=returned_quantity+? WHERE id=? AND loan_id=?", (quantity, item_id, loan_id))
            pending = db.execute("SELECT SUM(quantity-returned_quantity) FROM load_loan_items WHERE loan_id=?", (loan_id,)).fetchone()[0]
            status = "returned" if int(pending or 0) == 0 else "partial"
            db.execute(
                """UPDATE load_loans SET status=?,return_photo_data=CASE WHEN ?<>'' THEN ? ELSE return_photo_data END,
                   return_thumbnail_data=CASE WHEN ?<>'' THEN ? ELSE return_thumbnail_data END,
                   returned_by=?,returned_at=CASE WHEN ?='returned' THEN CURRENT_TIMESTAMP ELSE returned_at END,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, return_photo, return_photo, return_thumbnail, return_thumbnail, g.user["id"], status, loan_id),
            )
            db.execute(
                "INSERT INTO load_loan_history(loan_id,event_type,description,changed_by) VALUES(?,'return',?,?)",
                (loan_id, "Devolução: " + ", ".join(descriptions), g.user["id"]),
            )
        flash("Devolução registrada.", "success")
    except ValueError as exc:
        db.rollback(); flash(str(exc), "danger")
    except RequestEntityTooLarge:
        db.rollback(); raise
    except Exception:
        db.rollback(); current_app.logger.exception("Erro ao devolver empréstimo %s", loan_id)
        flash("Não foi possível registrar a devolução.", "danger")
    return redirect(url_for("infra.loan_detail", loan_id=loan_id))


@bp.post("/loans/<int:loan_id>/cancel")
@roles_allowed("manager", "infra")
def cancel_loan(loan_id):
    db = get_db()
    with db:
        changed = db.execute("UPDATE load_loans SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'", (loan_id,))
        if changed.rowcount:
            db.execute("INSERT INTO load_loan_history(loan_id,event_type,description,changed_by) VALUES(?,'cancel','Empréstimo cancelado.',?)", (loan_id, g.user["id"]))
    flash("Empréstimo cancelado." if changed.rowcount else "O empréstimo não pode ser cancelado.", "success" if changed.rowcount else "warning")
    return redirect(url_for("infra.loan_detail", loan_id=loan_id))


@bp.get("/load-relation/report.pdf")
@roles_allowed("manager", "infra")
def load_relation_report():
    query = request.args.get("q", "").strip()
    area_code = request.args.get("area", "").strip().upper()
    status = request.args.get("status", "").strip().lower()
    location = request.args.get("location", "").strip()
    responsible = request.args.get("responsible", "").strip()
    due = request.args.get("due", "").strip()
    try:
        material_id = int(request.args.get("material_id", ""))
    except (TypeError, ValueError):
        material_id = None
    material = get_db().execute("SELECT description FROM materials WHERE id=?", (material_id,)).fetchone() if material_id else None
    report_filter = " · ".join(value for value in (query, area_code, material["description"] if material else "", LOAD_STATUS_LABELS.get(status, ""), location, responsible, due) if value)
    report = build_load_relation_pdf(
        load_entry_rows(get_db(), query, area_code, status, location, responsible, due, material_id),
        local_today(), report_filter, material["description"] if material else "",
    )
    return send_file(
        report,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"relacao-de-carga-{local_today().isoformat()}.pdf",
    )


@bp.get("/load-relation/quantities.pdf")
@roles_allowed("manager", "infra")
def load_relation_quantities_pdf():
    """Export the filtered relation of load as a material quantity summary."""
    query = request.args.get("q", "").strip()
    area_code = request.args.get("area", "").strip().upper()
    status = request.args.get("status", "").strip().lower()
    location = request.args.get("location", "").strip()
    responsible = request.args.get("responsible", "").strip()
    due = request.args.get("due", "").strip()
    try:
        material_id = int(request.args.get("material_id", ""))
    except (TypeError, ValueError):
        material_id = None
    filters = " · ".join(value for value in (
        query, area_code, LOAD_STATUS_LABELS.get(status, ""), location, responsible, due,
    ) if value)
    report = build_load_quantity_pdf(
        load_entry_rows(get_db(), query, area_code, status, location, responsible, due, material_id),
        local_today(),
        filters,
    )
    return send_file(
        report,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"quantidade-por-material-{local_today().isoformat()}.pdf",
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
    try:
        material_id = int(request.args.get("material_id", ""))
    except (TypeError, ValueError):
        material_id = None
    db = get_db()
    entries = load_entry_rows(db, query, area_code, status, location, responsible, due, material_id)
    return render_template("load_report.html", entries=entries, total=len(entries), query=query,
                           area_code=area_code, status=status, location=location, responsible=responsible,
                           due=due, material_id=material_id, materials=material_options(db), load_areas=LOAD_AREAS, load_statuses=LOAD_STATUS_LABELS,
                           load_status_classes=LOAD_STATUS_CLASSES)
