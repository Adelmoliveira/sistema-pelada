"""Institutional membership only: never grants application roles or consumes turns."""
from datetime import date
import json

from src.utils import local_today

CATEGORIES = {'DIRETORIA': 'Diretoria', 'OFICIAL_GENERAL': 'Oficial General'}
OPERATORS = ('manager', 'football_manager')


class MembershipConflict(ValueError):
    pass


def _day(value, optional=False):
    value = (value or '').strip()
    if optional and not value:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Informe uma data válida no formato AAAA-MM-DD.') from None
    return value


def _snapshot(row):
    return json.dumps(dict(row) if row else {}, ensure_ascii=False, default=str, sort_keys=True)


def save_membership(db, actor_id, *, player_id=None, category=None, starts_on=None,
                    ends_on=None, reason='', membership_id=None, revision=None, end_only=False):
    """Owns the transaction. Serialize on player, then revalidate actor and revision."""
    try:
        if not db.is_postgres:
            db.execute('BEGIN IMMEDIATE')
        actor = db.execute('SELECT role,active FROM users WHERE id=?' +
                           (' FOR SHARE' if db.is_postgres else ''), (actor_id,)).fetchone()
        if not actor or not actor['active'] or actor['role'] not in OPERATORS:
            raise PermissionError('Operador não autorizado.')
        reason = (reason or '').strip()
        if not 3 <= len(reason) <= 1000:
            raise ValueError('Informe uma justificativa entre 3 e 1000 caracteres.')
        before = None
        if membership_id:
            before = db.execute('SELECT * FROM football_directorships WHERE id=?', (membership_id,)).fetchone()
            if not before:
                raise ValueError('Vínculo não encontrado.')
            player_id = before['player_id']
        try:
            player_id = int(player_id)
        except (TypeError, ValueError):
            raise ValueError('Selecione um peladeiro existente.') from None
        player = db.execute('SELECT id FROM players WHERE id=?' +
                            (' FOR UPDATE' if db.is_postgres else ''), (player_id,)).fetchone()
        if not player:
            raise ValueError('Peladeiro não encontrado.')
        if membership_id:
            before = db.execute('SELECT * FROM football_directorships WHERE id=?', (membership_id,)).fetchone()
            try:
                fresh = int(revision) == before['revision']
            except (ValueError, TypeError):
                fresh = False
            if not fresh:
                raise MembershipConflict('O vínculo foi alterado. Atualize a página antes de salvar.')
            if end_only:
                category, starts_on = before['category'], before['starts_on']
        if category not in CATEGORIES:
            raise ValueError('Categoria inválida.')
        starts_on, ends_on = _day(starts_on), _day(ends_on, optional=not end_only)
        if ends_on and ends_on < starts_on:
            raise ValueError('O encerramento não pode anteceder o início da vigência.')
        overlap = db.execute('''SELECT id FROM football_directorships WHERE player_id=?
            AND id<>? AND starts_on<=? AND ?<=COALESCE(ends_on,'9999-12-31')''',
            (player_id, membership_id or -1, ends_on or '9999-12-31', starts_on)).fetchone()
        if overlap:
            raise MembershipConflict('Já existe uma vigência sobreposta para este peladeiro, inclusive em outra categoria.')
        if membership_id:
            db.execute('''UPDATE football_directorships SET category=?,starts_on=?,ends_on=?,
                updated_by=?,updated_at=CURRENT_TIMESTAMP,change_reason=?,revision=revision+1 WHERE id=?''',
                (category, starts_on, ends_on, actor_id, reason, membership_id))
            event_type = 'ENDED' if end_only else 'AMENDED'
        else:
            membership_id = db.execute('''INSERT INTO football_directorships
                (player_id,category,starts_on,ends_on,created_by,updated_by,change_reason)
                VALUES(?,?,?,?,?,?,?)''',
                (player_id, category, starts_on, ends_on, actor_id, actor_id, reason)).lastrowid
            event_type = 'REGISTERED'
        after = db.execute('SELECT * FROM football_directorships WHERE id=?', (membership_id,)).fetchone()
        db.execute('''INSERT INTO football_benefit_events
            (directorship_id,player_id,event_type,actor_id,reason,before_snapshot,after_snapshot)
            VALUES(?,?,?,?,?,?,?)''',
            (membership_id, player_id, event_type, actor_id, reason, _snapshot(before), _snapshot(after)))
        db.commit()
        return membership_id
    except Exception:
        db.rollback()
        raise


def list_memberships(db):
    today = local_today().isoformat()
    return db.execute('''SELECT d.*,p.name,p.war_name,
        CASE WHEN d.starts_on>? THEN 'Agendado' WHEN d.ends_on IS NOT NULL AND d.ends_on<?
             THEN 'Encerrado' ELSE 'Ativo' END AS validity_status
        FROM football_directorships d JOIN players p ON p.id=d.player_id
        ORDER BY p.name,d.starts_on DESC,d.id DESC''', (today, today)).fetchall()
