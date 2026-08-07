import uuid
from datetime import date

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, send_file, url_for

from src.db import get_db
from src.routes.auth import roles_allowed
from src.routes.infra import LOAD_AREAS
from src.services.maintenance_pdf import build_maintenance_pdf
from src.services.material_photos import process_material_photo
from src.services.push_notifications import send_player_push
from src.utils import cents, local_today


bp = Blueprint("maintenance", __name__, url_prefix="/infra/maintenance")
MAX_PHASE_PHOTOS = 6
# Áreas disponíveis para chamados. "EXT" é exclusiva de manutenção e não
# altera as áreas usadas para os códigos patrimoniais da Relação de Carga.
MAINTENANCE_AREAS = {**LOAD_AREAS, "EXT": "Área externa"}
CATEGORIES = {
    "electrical": "Elétrica", "plumbing": "Hidráulica", "civil": "Civil",
    "painting": "Pintura", "equipment": "Equipamentos", "cleaning": "Limpeza",
    "other": "Outro",
}
PRIORITIES = {"low": "Baixa", "medium": "Média", "high": "Alta", "urgent": "Urgente"}
STATUSES = {
    "open": "Aberto", "analysis": "Em análise", "in_progress": "Em andamento",
    "waiting_material": "Aguardando material", "completed": "Concluído", "cancelled": "Cancelado",
}
STATUS_PUSH = {
    "open": ("Chamado recebido", "Seu chamado {code} foi recebido."),
    "analysis": ("Chamado em análise", "O chamado {code} está em análise."),
    "in_progress": ("Chamado em andamento", "O chamado {code} está em andamento."),
    "waiting_material": ("Chamado aguardando informação", "O chamado {code} aguarda informação ou material."),
    # Use the wording "finalizado" in the player-facing notification so it is
    # unambiguous that no further action is pending from the requester.
    "completed": ("Chamado finalizado", "Seu chamado {code} foi finalizado com sucesso."),
    "cancelled": ("Chamado cancelado", "O chamado {code} foi cancelado."),
}


class DuplicateMaintenanceRequest(ValueError):
    """Indica que já existe um chamado aberto para o mesmo assunto/área."""


def _record_history(db, request_id, status, responsible="", observation="", changed_by=None):
    db.execute(
        """INSERT INTO maintenance_request_history
           (request_id,status,responsible,observation,changed_by) VALUES(?,?,?,?,?)""",
        (request_id, status, responsible or "", observation or "", changed_by),
    )


def _push(db, player_id, title, body):
    if not player_id:
        return
    try:
        send_player_push(db, player_id, title, body, "/infra/maintenance/mine")
    except Exception as exc:
        current_app.logger.warning("Falha no push de manutenção para player=%s: %s", player_id, exc)


def _infra_player_ids(db):
    return [row["player_id"] for row in db.execute(
        "SELECT player_id FROM users WHERE role='infra' AND player_id IS NOT NULL"
    ).fetchall()]


def _valid_date(value, label, required=False):
    value = (value or "").strip()
    if not value and not required:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"Informe uma data válida para {label}.")


def _form_values():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    area_code = request.form.get("area_code", "").strip().upper()
    category = request.form.get("category", "")
    priority = request.form.get("priority", "")
    limited_access = g.user and g.user["role"] in ("maintenance", "staff", "client")
    status = "open" if limited_access else request.form.get("status", "open")
    if not title:
        raise ValueError("O título do problema é obrigatório.")
    if not description:
        raise ValueError("A descrição do problema é obrigatória.")
    if area_code not in MAINTENANCE_AREAS:
        raise ValueError("Selecione uma área válida.")
    if category not in CATEGORIES:
        raise ValueError("Selecione um tipo de manutenção válido.")
    if priority not in PRIORITIES:
        raise ValueError("Selecione uma prioridade válida.")
    if status not in STATUSES:
        raise ValueError("Selecione um status válido.")
    location = request.form.get("location", "").strip()
    responsible = "" if limited_access else request.form.get("responsible", "").strip()
    resolution = "" if limited_access else request.form.get("resolution", "").strip()
    notes = request.form.get("notes", "").strip()
    if len(title) > 200 or len(location) > 200 or len(responsible) > 150:
        raise ValueError("Um dos campos de identificação ultrapassou o tamanho permitido.")
    if any(len(value) > 5000 for value in (description, resolution, notes)):
        raise ValueError("Os textos devem ter no máximo 5.000 caracteres.")
    occurred_on = _valid_date(request.form.get("occurred_on"), "a ocorrência", required=True)
    due_on = "" if limited_access else _valid_date(request.form.get("due_on"), "a previsão")
    completed_on = "" if limited_access else _valid_date(request.form.get("completed_on"), "a conclusão")
    if status == "completed":
        if not resolution:
            raise ValueError("Descreva a resolução antes de concluir o chamado.")
        completed_on = completed_on or local_today().isoformat()
    else:
        completed_on = ""
    cost_cents = 0 if limited_access else cents(request.form.get("cost", "0"))
    if cost_cents < 0:
        raise ValueError("O custo não pode ser negativo.")
    return (
        title, area_code, location, category, priority, description, responsible,
        status, occurred_on, due_on, resolution, completed_on, cost_cents, notes,
    )


def _process_photos(files):
    files = [photo for photo in files if photo and photo.filename]
    if len(files) > MAX_PHASE_PHOTOS:
        raise ValueError(f"Envie no máximo {MAX_PHASE_PHOTOS} fotos em cada etapa.")
    return [process_material_photo(photo) for photo in files]


def _find_open_duplicate(db, title, area_code):
    """Localiza o chamado aberto mais recente para o mesmo assunto e área."""
    return db.execute(
        """SELECT mr.code,
                  COALESCE(NULLIF(p.war_name, ''), NULLIF(p.name, ''),
                           NULLIF(u.name, ''), 'outro usuário') AS requester
           FROM maintenance_requests mr
           LEFT JOIN users u ON u.id=mr.created_by
           LEFT JOIN players p ON p.id=u.player_id
          WHERE LOWER(TRIM(mr.title))=LOWER(TRIM(?))
            AND mr.area_code=?
            AND mr.status NOT IN ('completed','cancelled')
          ORDER BY mr.id DESC LIMIT 1""",
        (title, area_code),
    ).fetchone()


def _request_rows(db):
    sql = """SELECT mr.*,
                    (SELECT COUNT(*) FROM maintenance_photos mp WHERE mp.request_id=mr.id AND mp.phase='problem') problem_photo_count,
                    (SELECT COUNT(*) FROM maintenance_photos mp WHERE mp.request_id=mr.id AND mp.phase='resolution') resolution_photo_count
             FROM maintenance_requests mr"""
    conditions, params = [], []
    filters = {
        "area": ("mr.area_code=?", MAINTENANCE_AREAS),
        "category": ("mr.category=?", CATEGORIES),
        "priority": ("mr.priority=?", PRIORITIES),
        "status": ("mr.status=?", STATUSES),
    }
    selected = {}
    for name, (condition, options) in filters.items():
        value = request.args.get(name, "").strip()
        if name == "status" and "status" not in request.args:
            value = "open"
        selected[name] = value if value in options else ""
        if selected[name]:
            conditions.append(condition)
            params.append(selected[name])
    query = request.args.get("q", "").strip()
    if query:
        term = f"%{query.lower()}%"
        conditions.append("(LOWER(mr.code) LIKE ? OR LOWER(mr.title) LIKE ? OR LOWER(mr.location) LIKE ? OR LOWER(mr.responsible) LIKE ?)")
        params.extend((term, term, term, term))
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY CASE mr.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,mr.id DESC"
    return db.execute(sql, tuple(params)).fetchall(), selected, query


def _display_request_rows(rows):
    """Add presentation-only open-age data without changing database rows."""
    today = local_today()
    display_rows = []
    for row in rows:
        item = dict(row)
        if item["status"] not in ("completed", "cancelled"):
            try:
                opened_on = date.fromisoformat(str(item["created_at"])[:10])
                item["open_days"] = max(0, (today - opened_on).days)
            except (TypeError, ValueError):
                item["open_days"] = 0
        else:
            item["open_days"] = None
        display_rows.append(item)
    return display_rows


def _template_context():
    return dict(
        areas=MAINTENANCE_AREAS, categories=CATEGORIES, priorities=PRIORITIES,
        statuses=STATUSES, max_phase_photos=MAX_PHASE_PHOTOS,
    )


def _requester_name():
    """Nome exibido no formulário, sempre derivado da sessão autenticada."""
    if not g.user:
        return ""
    if g.user["role"] == "client" and g.user["player_id"]:
        player = get_db().execute(
            "SELECT war_name, name FROM players WHERE id=?", (g.user["player_id"],)
        ).fetchone()
        if player:
            return player["war_name"] or player["name"]
    return g.user["name"]


@bp.get("")
@roles_allowed("manager", "infra")
def requests_list():
    rows, selected, query = _request_rows(get_db())
    display_rows = _display_request_rows(rows)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 20
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = min(page, pages)
    context = _template_context()
    context.update(
        requests=display_rows[(page - 1) * per_page:page * per_page], total=len(display_rows),
        selected=selected, query=query, page=page, pages=pages, today=local_today().isoformat(),
    )
    return render_template("maintenance_list.html", **context)


@bp.get("/dashboard")
@roles_allowed("manager", "infra")
def dashboard():
    db = get_db()
    today = local_today().isoformat()
    month = local_today().strftime("%Y-%m")
    metrics = db.execute(
        """SELECT COUNT(CASE WHEN status NOT IN ('completed','cancelled') THEN 1 END) open_count,
                  COUNT(CASE WHEN priority='urgent' AND status NOT IN ('completed','cancelled') THEN 1 END) urgent_count,
                  COUNT(CASE WHEN due_on!='' AND due_on<? AND status NOT IN ('completed','cancelled') THEN 1 END) overdue_count,
                  COUNT(CASE WHEN status='completed' AND completed_on LIKE ? THEN 1 END) completed_month,
                  COALESCE(SUM(CASE WHEN completed_on LIKE ? THEN cost_cents ELSE 0 END),0) month_cost
           FROM maintenance_requests""",
        (today, f"{month}%", f"{month}%"),
    ).fetchone()
    by_status = db.execute(
        "SELECT status,COUNT(*) total FROM maintenance_requests GROUP BY status"
    ).fetchall()
    recent = db.execute(
        """SELECT * FROM maintenance_requests WHERE status NOT IN ('completed','cancelled')
           ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,id DESC LIMIT 10"""
    ).fetchall()
    return render_template(
        "maintenance_dashboard.html", metrics=metrics, by_status=by_status,
        recent=recent, statuses=STATUSES, priorities=PRIORITIES, areas=MAINTENANCE_AREAS,
        today=today,
    )


@bp.route("/new", methods=["GET", "POST"])
@roles_allowed("manager", "infra", "maintenance", "staff", "client")
def new_request():
    if request.method == "POST":
        try:
            values = _form_values()
            problem_photos = _process_photos(request.files.getlist("problem_photos"))
            # Peladeiros, assim como os perfis de abertura simplificada, não
            # podem acessar o acompanhamento interno do chamado. Após criar,
            # devem permanecer no formulário (sem passar pela rota de detalhes
            # restrita a gerente/infra).
            limited_access = g.user["role"] in ("maintenance", "staff", "client")
            resolution_photos = [] if limited_access else _process_photos(request.files.getlist("resolution_photos"))
            db = get_db()
            infra_ids = _infra_player_ids(db)
            duplicate = _find_open_duplicate(db, values[0], values[1])
            if duplicate:
                raise DuplicateMaintenanceRequest(
                    f"Já existe o chamado {duplicate['code']} sobre este assunto, "
                    f"aberto por {duplicate['requester']}. Verifique o andamento antes de abrir outro."
                )
            with db:
                cursor = db.execute(
                    """INSERT INTO maintenance_requests
                       (code,title,area_code,location,category,priority,description,responsible,status,
                        occurred_on,due_on,resolution,completed_on,cost_cents,notes,created_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"pending-{uuid.uuid4().hex}", *values, g.user["id"]),
                )
                request_id = cursor.lastrowid
                code = f"MAN-{request_id:06d}"
                db.execute("UPDATE maintenance_requests SET code=? WHERE id=?", (code, request_id))
                for photo, thumbnail in problem_photos:
                    db.execute(
                        "INSERT INTO maintenance_photos(request_id,phase,photo_data,thumbnail_data) VALUES(?,'problem',?,?)",
                        (request_id, photo, thumbnail),
                    )
                for photo, thumbnail in resolution_photos:
                    db.execute(
                        "INSERT INTO maintenance_photos(request_id,phase,photo_data,thumbnail_data) VALUES(?,'resolution',?,?)",
                        (request_id, photo, thumbnail),
                    )
                _record_history(db, request_id, "open", values[6], values[-1], g.user["id"])
            requester_player_id = g.user["player_id"] if g.user and g.user["player_id"] else None
            _push(
                db,
                requester_player_id,
                STATUS_PUSH["open"][0],
                STATUS_PUSH["open"][1].format(code=code),
            )
            for infra_id in infra_ids:
                _push(db, infra_id, "Novo chamado aberto", f"Chamado {code}: {values[0]}.")
            flash(f"Chamado {code} criado.", "success")
            if limited_access:
                return redirect(url_for("maintenance.new_request"))
            return redirect(url_for("maintenance.request_detail", request_id=request_id))
        except DuplicateMaintenanceRequest as exc:
            flash(str(exc), "warning")
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao criar chamado de manutenção: {exc}")
            flash("Erro interno ao criar o chamado.", "danger")
    return render_template(
        "maintenance_form.html", maintenance=None, photos=[], form_title="Novo chamado",
        today=local_today().isoformat(), requester_name=_requester_name(), **_template_context(),
    )


@bp.get("/<int:request_id>")
@roles_allowed("manager", "infra")
def request_detail(request_id):
    db = get_db()
    maintenance = db.execute(
        """SELECT mr.*,u.name created_by_name FROM maintenance_requests mr
           LEFT JOIN users u ON u.id=mr.created_by WHERE mr.id=?""", (request_id,)
    ).fetchone()
    if not maintenance:
        flash("Chamado não encontrado.", "warning")
        return redirect(url_for("maintenance.requests_list"))
    photos = db.execute(
        "SELECT * FROM maintenance_photos WHERE request_id=? ORDER BY phase,id", (request_id,)
    ).fetchall()
    history = db.execute(
        """SELECT h.*,u.name changed_by_name FROM maintenance_request_history h
           LEFT JOIN users u ON u.id=h.changed_by WHERE h.request_id=? ORDER BY h.changed_at DESC,h.id DESC""",
        (request_id,),
    ).fetchall()
    return render_template(
        "maintenance_detail.html", maintenance=maintenance, photos=photos,
        history=history,
        **_template_context(),
    )


@bp.get("/mine")
@roles_allowed("client")
def my_requests():
    db = get_db()
    rows = db.execute(
        """SELECT mr.*
           FROM maintenance_requests mr
           LEFT JOIN users creator ON creator.id=mr.created_by
           WHERE mr.created_by=?
              OR (? IS NOT NULL AND creator.player_id=?)
           ORDER BY mr.id DESC""",
        (g.user["id"], g.user["player_id"], g.user["player_id"]),
    ).fetchall()
    status_counts = {status: 0 for status in STATUSES}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    return render_template(
        "maintenance_my_requests.html", requests=_display_request_rows(rows),
        status_counts=status_counts,
        **_template_context(),
    )


@bp.get("/mine/<int:request_id>")
@roles_allowed("client")
def my_request_detail(request_id):
    """Exibe um chamado do peladeiro em modo somente leitura.

    A consulta repete a mesma regra de vínculo da lista para impedir que um
    peladeiro consiga descobrir ou visualizar chamados de outra pessoa.
    """
    db = get_db()
    maintenance = db.execute(
        """SELECT mr.*,u.name created_by_name
           FROM maintenance_requests mr
           LEFT JOIN users u ON u.id=mr.created_by
           WHERE mr.id=? AND (mr.created_by=?
             OR (? IS NOT NULL AND u.player_id=?))""",
        (request_id, g.user["id"], g.user["player_id"], g.user["player_id"]),
    ).fetchone()
    if not maintenance:
        flash("Chamado não encontrado.", "warning")
        return redirect(url_for("maintenance.my_requests"))
    photos = db.execute(
        "SELECT * FROM maintenance_photos WHERE request_id=? ORDER BY phase,id", (request_id,)
    ).fetchall()
    history = db.execute(
        """SELECT h.*,u.name changed_by_name FROM maintenance_request_history h
           LEFT JOIN users u ON u.id=h.changed_by
           WHERE h.request_id=? ORDER BY h.changed_at DESC,h.id DESC""",
        (request_id,),
    ).fetchall()
    return render_template(
        "maintenance_my_request_detail.html", maintenance=maintenance,
        photos=photos, history=history, **_template_context(),
    )


@bp.route("/<int:request_id>/edit", methods=["GET", "POST"])
@roles_allowed("manager", "infra")
def edit_request(request_id):
    db = get_db()
    maintenance = db.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone()
    if not maintenance:
        flash("Chamado não encontrado.", "warning")
        return redirect(url_for("maintenance.requests_list"))
    photos = db.execute("SELECT * FROM maintenance_photos WHERE request_id=? ORDER BY phase,id", (request_id,)).fetchall()
    if request.method == "POST":
        try:
            values = _form_values()
            remove_ids = {int(value) for value in request.form.getlist("remove_photo_ids")}
            valid_ids = {photo["id"] for photo in photos}
            remove_ids &= valid_ids
            problem_photos = _process_photos(request.files.getlist("problem_photos"))
            resolution_photos = _process_photos(request.files.getlist("resolution_photos"))
            remaining = {phase: sum(1 for photo in photos if photo["phase"] == phase and photo["id"] not in remove_ids) for phase in ("problem", "resolution")}
            if remaining["problem"] + len(problem_photos) > MAX_PHASE_PHOTOS or remaining["resolution"] + len(resolution_photos) > MAX_PHASE_PHOTOS:
                raise ValueError(f"Cada etapa pode possuir no máximo {MAX_PHASE_PHOTOS} fotos.")
            changed = any(
                maintenance[field] != values[index]
                for field, index in {
                    "title": 0, "area_code": 1, "location": 2, "category": 3,
                    "priority": 4, "description": 5, "responsible": 6, "status": 7,
                    "occurred_on": 8, "due_on": 9, "resolution": 10,
                    "completed_on": 11, "cost_cents": 12, "notes": 13,
                }.items()
            ) or bool(remove_ids or problem_photos or resolution_photos)
            with db:
                db.execute(
                    """UPDATE maintenance_requests SET title=?,area_code=?,location=?,category=?,priority=?,
                       description=?,responsible=?,status=?,occurred_on=?,due_on=?,resolution=?,completed_on=?,
                       cost_cents=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (*values, request_id),
                )
                for photo_id in remove_ids:
                    db.execute("DELETE FROM maintenance_photos WHERE id=? AND request_id=?", (photo_id, request_id))
                for phase, processed in (("problem", problem_photos), ("resolution", resolution_photos)):
                    for photo, thumbnail in processed:
                        db.execute(
                            "INSERT INTO maintenance_photos(request_id,phase,photo_data,thumbnail_data) VALUES(?,?,?,?)",
                            (request_id, phase, photo, thumbnail),
                        )
                if changed:
                    _record_history(db, request_id, values[7], values[6], values[-1] or values[10], g.user["id"])
            if changed:
                creator = db.execute("SELECT player_id FROM users WHERE id=?", (maintenance["created_by"],)).fetchone()
                player_id = creator["player_id"] if creator and creator["player_id"] else None
                if maintenance["status"] != values[7]:
                    title, body = STATUS_PUSH[values[7]]
                    body = body.format(code=maintenance["code"])
                else:
                    title, body = "Chamado atualizado", f"O chamado {maintenance['code']} recebeu uma atualização."
                _push(db, player_id, title, body)
            flash("Chamado atualizado.", "success")
            return redirect(url_for("maintenance.request_detail", request_id=request_id))
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            current_app.logger.error(f"Erro ao editar chamado {request_id}: {exc}")
            flash("Erro interno ao atualizar o chamado.", "danger")
        maintenance = db.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,)).fetchone()
        photos = db.execute("SELECT * FROM maintenance_photos WHERE request_id=? ORDER BY phase,id", (request_id,)).fetchall()
    return render_template(
        "maintenance_form.html", maintenance=maintenance, photos=photos,
        form_title=f"Editar {maintenance['code']}", today=local_today().isoformat(),
        **_template_context(),
    )


@bp.post("/<int:request_id>/delete")
@roles_allowed("manager", "infra")
def delete_request(request_id):
    db = get_db()
    deleted = db.execute("DELETE FROM maintenance_requests WHERE id=?", (request_id,))
    db.commit()
    flash("Chamado apagado." if deleted.rowcount else "Chamado não encontrado.", "success" if deleted.rowcount else "warning")
    return redirect(url_for("maintenance.requests_list"))


@bp.get("/report.pdf")
@roles_allowed("manager", "infra")
def report():
    rows, selected, query = _request_rows(get_db())
    filter_values = [query, selected["area"], selected["category"], selected["priority"], selected["status"]]
    report_data = build_maintenance_pdf(
        rows, local_today(), " · ".join(value for value in filter_values if value),
        CATEGORIES, PRIORITIES, STATUSES,
    )
    return send_file(
        report_data, mimetype="application/pdf", as_attachment=True,
        download_name=f"manutencao-{local_today().isoformat()}.pdf",
    )
