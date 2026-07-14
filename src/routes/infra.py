from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.services.material_photos import process_material_photo
from src.utils import alphabetical_key


bp = Blueprint("infra", __name__, url_prefix="/infra")


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


@bp.get("/materials")
@roles_allowed("manager", "staff")
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
@roles_allowed("manager", "staff")
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
@roles_allowed("manager", "staff")
def material_detail(material_id):
    material = get_db().execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    if not material:
        flash("Material não encontrado.", "warning")
        return redirect(url_for("infra.materials"))
    return render_template("material_detail.html", material=material)


@bp.route("/materials/<int:material_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "staff")
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
@roles_allowed("manager", "staff")
def delete_material(material_id):
    db = get_db()
    try:
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
@roles_allowed("manager", "staff")
def load_relation():
    return render_template("load_relation.html")
