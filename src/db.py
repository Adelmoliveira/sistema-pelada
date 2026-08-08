import os
import sqlite3
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import g, current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    password_required INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL CHECK(role IN ('manager','staff','client','infra','maintenance','display','football_manager')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens(user_id,used_at,expires_at);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    war_name TEXT DEFAULT '',
    cpf TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    emergency_phone TEXT DEFAULT '',
    gender TEXT NOT NULL DEFAULT 'male',
    birth_date TEXT DEFAULT '',
    postal_code TEXT DEFAULT '',
    address_street TEXT DEFAULT '',
    address_number TEXT DEFAULT '',
    address_complement TEXT DEFAULT '',
    address_neighborhood TEXT DEFAULT '',
    address_city TEXT DEFAULT '',
    address_state TEXT DEFAULT '',
    email TEXT DEFAULT '',
    membership_type TEXT NOT NULL DEFAULT 'regular',
    photo_data TEXT DEFAULT '',
    thumbnail_data TEXT DEFAULT '',
    football_position TEXT DEFAULT '',
    football_join_date TEXT DEFAULT '',
    club_qr_data TEXT DEFAULT '',
    club_qr_token TEXT DEFAULT '',
    club_qr_updated_at TEXT,
    historical_only INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    package_type TEXT NOT NULL DEFAULT '',
    units_per_case INTEGER NOT NULL DEFAULT 0 CHECK(units_per_case >= 0),
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
    stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
    min_stock INTEGER NOT NULL DEFAULT 5 CHECK(min_stock >= 0),
    supplier_email TEXT DEFAULT '',
    photo_data TEXT DEFAULT '',
    thumbnail_data TEXT DEFAULT '',
    expiry_date TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bar_restock_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_by INTEGER NOT NULL REFERENCES users(id),
    cleaning_materials TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK(status IN ('PENDENTE','VISTA','ATENDIDA','CANCELADA')),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    review_notes TEXT DEFAULT '',
    workflow_status TEXT NOT NULL DEFAULT 'PENDENTE',
    supplier TEXT NOT NULL DEFAULT '',
    purchase_amount_cents INTEGER NOT NULL DEFAULT 0,
    payment_account TEXT NOT NULL DEFAULT 'bank',
    receipt_data TEXT NOT NULL DEFAULT '',
    receipt_filename TEXT NOT NULL DEFAULT '',
    receipt_mime TEXT NOT NULL DEFAULT '',
    purchase_recorded_at TEXT,
    purchase_recorded_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bar_restock_request_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    changed_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bar_restock_history_request ON bar_restock_request_history(request_id,created_at);
CREATE TABLE IF NOT EXISTS bar_restock_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bar_restock_notifications_user ON bar_restock_notifications(user_id,read_at,created_at);
CREATE TABLE IF NOT EXISTS bar_restock_request_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    measure TEXT NOT NULL CHECK(measure IN ('caixas','unidades')),
    description TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bar_restock_requests_status ON bar_restock_requests(status,created_at);
CREATE TABLE IF NOT EXISTS bar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    event_date TEXT DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_bar_events_status_date ON bar_events(status,event_date,id);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER REFERENCES players(id),
    event_id INTEGER REFERENCES bar_events(id),
    guest_name TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia','Créditos')),
    total_cents INTEGER NOT NULL,
    paid INTEGER NOT NULL DEFAULT 1,
    payment_status TEXT NOT NULL DEFAULT 'approved',
    mercadopago_order_id TEXT,
    mercadopago_payment_id TEXT,
    external_reference TEXT,
    idempotency_key TEXT,
    paid_at TEXT,
    ready_for_delivery INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivered_by INTEGER REFERENCES users(id),
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_price_cents INTEGER NOT NULL,
    unit_cost_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bar_credit_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL UNIQUE REFERENCES players(id) ON DELETE CASCADE,
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0),
    low_balance_notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    ,last_push_at TEXT
    ,last_push_status TEXT NOT NULL DEFAULT 'never'
    ,last_push_error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bar_credit_topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    payment_method TEXT NOT NULL DEFAULT 'Pix',
    paid INTEGER NOT NULL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'creating',
    mercadopago_order_id TEXT,
    mercadopago_payment_id TEXT,
    external_reference TEXT UNIQUE,
    idempotency_key TEXT,
    paid_at TEXT,
    refunded_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bar_credit_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('PURCHASE','CONSUMPTION','ADJUSTMENT','REFUND')),
    amount_cents INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sale_id INTEGER REFERENCES sales(id) ON DELETE SET NULL,
    topup_id INTEGER REFERENCES bar_credit_topups(id) ON DELETE SET NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bar_credit_transactions_player ON bar_credit_transactions(player_id,created_at);
CREATE TABLE IF NOT EXISTS bar_credit_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    topup_id INTEGER REFERENCES bar_credit_topups(id) ON DELETE SET NULL,
    transaction_id INTEGER REFERENCES bar_credit_transactions(id) ON DELETE SET NULL,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bar_credit_audit_player ON bar_credit_audit(player_id,created_at);
CREATE TABLE IF NOT EXISTS sale_item_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_item_id INTEGER NOT NULL REFERENCES sale_items(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    delivered_by INTEGER REFERENCES users(id),
    delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sale_item_deliveries_item ON sale_item_deliveries(sale_item_id);
CREATE TABLE IF NOT EXISTS sale_cancellations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL UNIQUE REFERENCES sales(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    canceled_by INTEGER REFERENCES users(id),
    canceled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sale_cancellations_date ON sale_cancellations(canceled_at);
CREATE TABLE IF NOT EXISTS restocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cash_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_date TEXT NOT NULL UNIQUE,
    opening_cash_cents INTEGER NOT NULL DEFAULT 0 CHECK(opening_cash_cents >= 0),
    opening_bank_cents INTEGER NOT NULL DEFAULT 0 CHECK(opening_bank_cents >= 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    opened_by INTEGER REFERENCES users(id),
    opened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    counted_cash_cents INTEGER,
    counted_bank_cents INTEGER,
    expected_cash_cents INTEGER,
    expected_bank_cents INTEGER,
    cash_difference_cents INTEGER,
    bank_difference_cents INTEGER,
    closing_notes TEXT DEFAULT '',
    closed_by INTEGER REFERENCES users(id),
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES cash_sessions(id),
    account TEXT NOT NULL CHECK(account IN ('cash','bank')),
    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
    category TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    description TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    source_id INTEGER,
    created_by INTEGER REFERENCES users(id),
    reversed_movement_id INTEGER REFERENCES cash_movements(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source,source_id)
);
CREATE TABLE IF NOT EXISTS cash_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES cash_sessions(id),
    from_account TEXT NOT NULL CHECK(from_account IN ('cash','bank')),
    to_account TEXT NOT NULL CHECK(to_account IN ('cash','bank')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    description TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    reversed_at TEXT,
    reversed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(from_account <> to_account)
);
CREATE INDEX IF NOT EXISTS idx_cash_sessions_date ON cash_sessions(business_date);
CREATE INDEX IF NOT EXISTS idx_cash_movements_session ON cash_movements(session_id,created_at);
CREATE INDEX IF NOT EXISTS idx_cash_transfers_session ON cash_transfers(session_id,created_at);
CREATE TABLE IF NOT EXISTS restock_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restock_id INTEGER NOT NULL REFERENCES restocks(id),
    previous_quantity INTEGER NOT NULL CHECK(previous_quantity >= 0),
    corrected_quantity INTEGER NOT NULL CHECK(corrected_quantity >= 0),
    previous_unit_cost_cents INTEGER NOT NULL CHECK(previous_unit_cost_cents >= 0),
    corrected_unit_cost_cents INTEGER NOT NULL CHECK(corrected_unit_cost_cents >= 0),
    reason TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_restock_corrections_restock ON restock_corrections(restock_id,id);
CREATE TABLE IF NOT EXISTS stock_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    user_id INTEGER REFERENCES users(id),
    previous_stock INTEGER NOT NULL CHECK(previous_stock >= 0),
    new_stock INTEGER NOT NULL CHECK(new_stock >= 0),
    difference INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stock_conferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conference_month TEXT NOT NULL UNIQUE,
    notes TEXT NOT NULL DEFAULT '',
    performed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stock_conference_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conference_id INTEGER NOT NULL REFERENCES stock_conferences(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    expected_stock INTEGER NOT NULL CHECK(expected_stock >= 0),
    physical_stock INTEGER NOT NULL CHECK(physical_stock >= 0),
    difference INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stock_conferences_month ON stock_conferences(conference_month);
CREATE INDEX IF NOT EXISTS idx_stock_conference_items_conference ON stock_conference_items(conference_id);
CREATE TABLE IF NOT EXISTS stock_conference_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conference_month TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stock_conference_audit_month ON stock_conference_audit(conference_month,created_at);
CREATE TABLE IF NOT EXISTS stock_alert_states (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    alerted INTEGER NOT NULL DEFAULT 0,
    last_stock INTEGER NOT NULL DEFAULT 0,
    last_notified_at TEXT
);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    load_sheet TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    photo_data TEXT DEFAULT '',
    thumbnail_data TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS load_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    bmp TEXT NOT NULL UNIQUE,
    area_code TEXT NOT NULL DEFAULT 'BAR' CHECK(area_code IN ('BAR','COZ','SAL','HIS','VES','BAN')),
    serial_number TEXT DEFAULT '',
    location TEXT DEFAULT '',
    responsible TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','maintenance','discharged','lost','borrowed')),
    discharged_at TEXT,
    discharged_by INTEGER REFERENCES users(id),
    last_checked_at TEXT,
    last_checked_by INTEGER REFERENCES users(id),
    next_check_due_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS load_entry_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    load_entry_id INTEGER NOT NULL REFERENCES load_entries(id) ON DELETE CASCADE,
    photo_data TEXT NOT NULL,
    thumbnail_data TEXT NOT NULL,
    photo_kind TEXT NOT NULL DEFAULT 'registration',
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captured_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_load_entries_material ON load_entries(material_id);
CREATE INDEX IF NOT EXISTS idx_load_photos_entry ON load_entry_photos(load_entry_id);
CREATE TABLE IF NOT EXISTS load_entry_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    load_entry_id INTEGER NOT NULL REFERENCES load_entries(id) ON DELETE CASCADE,
    from_location TEXT DEFAULT '',
    to_location TEXT DEFAULT '',
    from_responsible TEXT DEFAULT '',
    to_responsible TEXT DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    moved_by INTEGER REFERENCES users(id),
    moved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_load_movements_entry ON load_entry_movements(load_entry_id,moved_at);
CREATE TABLE IF NOT EXISTS maintenance_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    area_code TEXT NOT NULL CHECK(area_code IN ('BAR','COZ','SAL','HIS','VES','BAN','EXT')),
    location TEXT DEFAULT '',
    category TEXT NOT NULL CHECK(category IN ('electrical','plumbing','civil','painting','equipment','cleaning','other')),
    priority TEXT NOT NULL CHECK(priority IN ('low','medium','high','urgent')),
    description TEXT NOT NULL,
    responsible TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','analysis','in_progress','waiting_material','completed','cancelled')),
    occurred_on TEXT NOT NULL,
    due_on TEXT,
    resolution TEXT DEFAULT '',
    completed_on TEXT,
    cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
    notes TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS maintenance_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES maintenance_requests(id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK(phase IN ('problem','resolution')),
    photo_data TEXT NOT NULL,
    thumbnail_data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_maintenance_status ON maintenance_requests(status);
CREATE INDEX IF NOT EXISTS idx_maintenance_area ON maintenance_requests(area_code);
CREATE INDEX IF NOT EXISTS idx_maintenance_photos_request ON maintenance_photos(request_id);
CREATE TABLE IF NOT EXISTS maintenance_request_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES maintenance_requests(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    responsible TEXT DEFAULT '',
    observation TEXT DEFAULT '',
    changed_by INTEGER REFERENCES users(id),
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_maintenance_history_request ON maintenance_request_history(request_id, changed_at);
CREATE TABLE IF NOT EXISTS membership_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    months_count INTEGER NOT NULL CHECK(months_count BETWEEN 1 AND 12),
    start_month TEXT NOT NULL,
    payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito')),
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS membership_months (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES membership_payments(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    month TEXT NOT NULL,
    UNIQUE(player_id, month)
);
CREATE TABLE IF NOT EXISTS finance_accounts (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    opening_cash_cents INTEGER NOT NULL DEFAULT 0 CHECK(opening_cash_cents >= 0),
    opening_bank_cents INTEGER NOT NULL DEFAULT 0 CHECK(opening_bank_cents >= 0),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS finance_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL CHECK(account IN ('cash','bank')),
    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
    category TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    description TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    source_id INTEGER,
    created_by INTEGER REFERENCES users(id),
    reversed_movement_id INTEGER REFERENCES finance_movements(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source,source_id)
);
CREATE TABLE IF NOT EXISTS interaccount_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cash_session_id INTEGER NOT NULL REFERENCES cash_sessions(id),
    direction TEXT NOT NULL CHECK(direction IN ('finance_to_bar','bar_to_finance')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    description TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    reversed_at TEXT,
    reversed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_finance_movements_created ON finance_movements(created_at);
CREATE INDEX IF NOT EXISTS idx_interaccount_transfers_created ON interaccount_transfers(created_at);
CREATE TABLE IF NOT EXISTS reminder_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enabled INTEGER NOT NULL DEFAULT 0,
    push_enabled INTEGER NOT NULL DEFAULT 1,
    schedule_day INTEGER NOT NULL DEFAULT 5 CHECK(schedule_day BETWEEN 1 AND 28),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    body_html TEXT DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reminder_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    period TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('sent','failed')),
    error_message TEXT DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(player_id, period)
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_player ON push_subscriptions(player_id);
CREATE TABLE IF NOT EXISTS push_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    period TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(player_id, kind, period)
);
CREATE TABLE IF NOT EXISTS push_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    image_url TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_inbox_player ON push_inbox(player_id, created_at);
CREATE TABLE IF NOT EXISTS push_announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    audience TEXT NOT NULL DEFAULT 'all',
    status TEXT NOT NULL DEFAULT 'ENVIADO',
    sent_count INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tribute_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT 'PELADEIROS GPCTA',
    body TEXT NOT NULL DEFAULT '🗣️ VEEENHAAAMMM...',
    body_html TEXT NOT NULL DEFAULT '🗣️ VEEENHAAAMMM...',
    image_data TEXT NOT NULL DEFAULT '',
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO tribute_settings(id) VALUES(1);
CREATE TABLE IF NOT EXISTS tribute_schedules (
    weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
    enabled INTEGER NOT NULL DEFAULT 0,
    hour INTEGER NOT NULL DEFAULT 12 CHECK(hour BETWEEN 0 AND 23),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO tribute_schedules(weekday,enabled,hour) VALUES(2,1,17);
INSERT OR IGNORE INTO tribute_schedules(weekday,enabled,hour) VALUES(5,1,15);
CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);

-- Módulo Futebol: súmulas digitais e histórico auditável.
CREATE TABLE IF NOT EXISTS football_sumulas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_date TEXT NOT NULL,
    day_pelada TEXT NOT NULL CHECK(day_pelada IN ('QUARTA','SABADO')),
    local TEXT NOT NULL DEFAULT '',
    horario TEXT NOT NULL DEFAULT '',
    situacao TEXT NOT NULL DEFAULT 'RASCUNHO' CHECK(situacao IN ('RASCUNHO','ABERTA','EM_ANDAMENTO','FINALIZADA','CANCELADA')),
    observacoes TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finalized_at TEXT,
    locked_at TEXT,
    locked_by INTEGER REFERENCES users(id),
    canceled_at TEXT,
    canceled_by INTEGER REFERENCES users(id),
    reopen_justification TEXT DEFAULT '',
    UNIQUE(match_date)
);
CREATE TABLE IF NOT EXISTS football_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    status TEXT NOT NULL DEFAULT 'CONFIRMADO' CHECK(status IN ('CONFIRMADO','AUSENTE','DESISTENTE','RESERVA')),
    preferred_position TEXT DEFAULT '',
    draw_order INTEGER,
    observation TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sumula_id, player_id)
);
CREATE TABLE IF NOT EXISTS football_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 3),
    starts_at TEXT,
    ends_at TEXT,
    blue_score INTEGER NOT NULL DEFAULT 0 CHECK(blue_score >= 0),
    white_score INTEGER NOT NULL DEFAULT 0 CHECK(white_score >= 0),
    status TEXT NOT NULL DEFAULT 'PLANEJADA' CHECK(status IN ('PLANEJADA','EM_ANDAMENTO','ENCERRADA','CANCELADA')),
    observation TEXT DEFAULT '',
    UNIQUE(sumula_id, number)
);
CREATE TABLE IF NOT EXISTS football_responsibles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
    match_id INTEGER REFERENCES football_matches(id) ON DELETE SET NULL,
    player_id INTEGER REFERENCES players(id),
    responsibility_type TEXT NOT NULL CHECK(responsibility_type IN ('SORTEIO','SUMULA','QUADRO','GOLEIRO_VOLUNTARIO','ARBITRO_VOLUNTARIO','OUTRO')),
    observation TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS football_participant_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    status TEXT NOT NULL DEFAULT 'CONFIRMADO' CHECK(status IN ('CONFIRMADO','AUSENTE','DESISTENTE','RESERVA')),
    draw_order INTEGER,
    observation TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sumula_id, match_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_football_participant_matches_sumula ON football_participant_matches(sumula_id,match_id);
CREATE TABLE IF NOT EXISTS football_lineups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    team TEXT NOT NULL CHECK(team IN ('AZUL','BRANCO')),
    position TEXT NOT NULL CHECK(position IN ('GOLEIRO','DEFENSOR','MEIO_CAMPO','ATACANTE')),
    slot TEXT DEFAULT '',
    titular INTEGER NOT NULL DEFAULT 1,
    draw_order INTEGER,
    observation TEXT DEFAULT '',
    period INTEGER NOT NULL DEFAULT 1 CHECK(period IN (1,2)),
    UNIQUE(match_id, player_id, period)
);
CREATE TABLE IF NOT EXISTS football_goalkeepers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id),
    team TEXT NOT NULL CHECK(team IN ('AZUL','BRANCO')),
    principal INTEGER NOT NULL DEFAULT 1,
    observation TEXT DEFAULT '',
    UNIQUE(match_id, player_id, team)
);
CREATE TABLE IF NOT EXISTS football_referees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
    player_id INTEGER REFERENCES players(id),
    function TEXT NOT NULL CHECK(function IN ('PRINCIPAL','AUXILIAR','MESARIO')),
    observation TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS football_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
    author_player_id INTEGER REFERENCES players(id),
    benefited_team TEXT NOT NULL CHECK(benefited_team IN ('AZUL','BRANCO')),
    assist_player_id INTEGER REFERENCES players(id),
    minute INTEGER,
    goal_type TEXT NOT NULL DEFAULT 'NORMAL',
    own_goal INTEGER NOT NULL DEFAULT 0,
    observation TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS football_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
    match_id INTEGER REFERENCES football_matches(id) ON DELETE SET NULL,
    type TEXT NOT NULL CHECK(type IN ('DISCIPLINAR','LESAO','ATRASO','ABANDONO_PARTIDA','DISCUSSAO','FALHA_ORGANIZACAO','PROBLEMA_ESTRUTURAL','OUTRO')),
    level TEXT NOT NULL DEFAULT 'INFORMATIVO' CHECK(level IN ('INFORMATIVO','ATENCAO','GRAVE')),
    player_id INTEGER REFERENCES players(id),
    card TEXT DEFAULT '' CHECK(card IN ('','AMARELO','AZUL','VERMELHO')),
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ABERTA' CHECK(status IN ('ABERTA','EM_ANALISE','RESOLVIDA','ARQUIVADA')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS football_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    details TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS football_deleted_sumula_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sumula_id INTEGER NOT NULL,
    match_date TEXT NOT NULL,
    day_pelada TEXT NOT NULL,
    local TEXT DEFAULT '',
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS football_historical_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    stat_date TEXT NOT NULL,
    goals INTEGER NOT NULL DEFAULT 0 CHECK(goals >= 0),
    assists INTEGER NOT NULL DEFAULT 0 CHECK(assists >= 0),
    notes TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(goals > 0 OR assists > 0)
);
CREATE TABLE IF NOT EXISTS football_transfer_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    window_year INTEGER NOT NULL,
    current_position TEXT NOT NULL DEFAULT '',
    requested_position TEXT NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK(status IN ('PENDENTE','APROVADA','RECUSADA')),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    review_notes TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(player_id, window_year)
);
CREATE INDEX IF NOT EXISTS idx_football_sumulas_date ON football_sumulas(match_date);
CREATE INDEX IF NOT EXISTS idx_football_participants_sumula ON football_participants(sumula_id);
CREATE INDEX IF NOT EXISTS idx_football_matches_sumula ON football_matches(sumula_id,number);
CREATE INDEX IF NOT EXISTS idx_football_incidents_sumula ON football_incidents(sumula_id,created_at);
CREATE INDEX IF NOT EXISTS idx_football_historical_player ON football_historical_stats(player_id,stat_date);
CREATE INDEX IF NOT EXISTS idx_football_transfer_status ON football_transfer_requests(status,window_year);
CREATE TABLE IF NOT EXISTS football_transfer_window_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    is_open INTEGER NOT NULL DEFAULT 0,
    manual_override INTEGER NOT NULL DEFAULT 0,
    window_year INTEGER,
    updated_by INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

class CursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @lastrowid.setter
    def lastrowid(self, val):
        self._lastrowid = val

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        self.cursor.close()

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def __iter__(self):
        return iter(self.cursor)

class DbWrapper:
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres

    def execute(self, sql, params=None):
        if self.is_postgres:
            sql_clean = sql.replace('?', '%s')
            
            is_insert = sql_clean.strip().upper().startswith('INSERT')
            if is_insert and 'RETURNING' not in sql_clean.upper():
                sql_clean += ' RETURNING id'

            cursor = self.conn.cursor()
            cursor.execute(sql_clean, params)
            
            wrapped = CursorWrapper(cursor)
            if is_insert:
                try:
                    row = cursor.fetchone()
                    if row:
                        wrapped.lastrowid = row[0]
                except Exception:
                    pass
            return wrapped
        else:
            cursor = self.conn.execute(sql, params or ())
            wrapped = CursorWrapper(cursor)
            wrapped.lastrowid = cursor.lastrowid
            return wrapped

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()

def get_db():
    if "db" not in g:
        g.db = connect_db(current_app)
    return g.db


TRANSIENT_POSTGRES_SQLSTATES = {"40001", "40P01"}


def is_transient_database_error(exc):
    """Return True only for retryable PostgreSQL concurrency failures."""
    sqlstate = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    if sqlstate in TRANSIENT_POSTGRES_SQLSTATES:
        return True
    # Supabase/PostgreSQL may surface this catalog-update race without a
    # populated SQLSTATE in the serverless log.
    return "tuple concurrently updated" in str(exc).lower()


def read_user_from_session(user_id, retries=2):
    """Read an authenticated user without changing schema or user state.

    A short retry is limited to PostgreSQL's transient serialization/catalog
    races. Every failed attempt is rolled back before another query is sent.
    """
    db = get_db()
    sql = "SELECT * FROM users WHERE id=? AND active=1"
    for attempt in range(retries + 1):
        try:
            return db.execute(sql, (user_id,)).fetchone()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            if not is_transient_database_error(exc) or attempt >= retries:
                raise
            time.sleep(0.05 * (attempt + 1))


def run_postgres_migrations(database_url):
    """Run the complete PostgreSQL schema setup explicitly, outside HTTP.

    This is intentionally callable from a deploy/maintenance command only;
    ``connect_db`` must never invoke it for a normal request.
    """
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        database_url,
        sslmode="require",
        connect_timeout=10,
        cursor_factory=psycopg2.extras.DictCursor,
    )
    try:
        # Protect an explicitly-run migration from two release jobs touching
        # PostgreSQL's catalog at the same time.
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext('sistema-pelada-schema'))")
        wrapper = DbWrapper(conn, is_postgres=True)
        init_postgres(wrapper)
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtext('sistema-pelada-schema'))")
        except Exception:
            pass
        conn.close()

def connect_db(app):
    db_url = os.environ.get("DATABASE_URL") or app.config.get("DATABASE_URL")
    if not db_url:
        # Desenvolvimento local: usa o SQLite já configurado pela aplicação.
        # Na Vercel o filesystem é temporário, portanto o Supabase continua
        # obrigatório para evitar perda silenciosa de dados em produção.
        if os.environ.get("VERCEL") or os.environ.get("NOW_REGION"):
            raise RuntimeError("DATABASE_URL não configurada. Defina a URL do Supabase no ambiente da aplicação.")
        database_path = app.config.get("DATABASE")
        if not database_path:
            raise RuntimeError("Banco local não configurado. Defina DATABASE ou DATABASE_URL.")
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        sao_paulo = ZoneInfo("America/Sao_Paulo")
        def local_date(value):
            try:
                parsed = datetime.fromisoformat(str(value))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(sao_paulo).date().isoformat()
            except (TypeError, ValueError):
                return None
        conn.create_function("date", 1, local_date)
        wrapper = DbWrapper(conn, is_postgres=False)
        init_sqlite(wrapper)
        return wrapper

    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        raise RuntimeError("DATABASE_URL inválida. Use uma URL PostgreSQL do Supabase.")

    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(
        db_url,
        sslmode="require",
        connect_timeout=10,
        cursor_factory=psycopg2.extras.DictCursor
    )
    with conn.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'UTC'")
    # PostgreSQL schema creation/migrations are deliberately not run here.
    # Vercel may create several instances concurrently; running DDL and data
    # UPDATEs from each request can make PostgreSQL report "tuple concurrently
    # updated". Run ``scripts/migrate_postgres_schema.py`` explicitly during
    # deployment/maintenance instead.
    return DbWrapper(conn, is_postgres=True)

def migrate_payment_method(connection):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sales'"
    ).fetchone()
    if not row or "'Créditos'" in (row[0] or ""):
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript("""
        BEGIN;
        ALTER TABLE sale_items RENAME TO sale_items_old;
        ALTER TABLE sales RENAME TO sales_old;
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia','Créditos')),
            total_cents INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price_cents INTEGER NOT NULL,
            unit_cost_cents INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO sales(id,player_id,payment_method,total_cents,paid,notes,created_at)
        SELECT id,player_id,CASE WHEN payment_method='Fiado' THEN 'Débito' ELSE payment_method END,
               total_cents,1,notes,created_at FROM sales_old;
        INSERT INTO sale_items SELECT * FROM sale_items_old;
        DROP TABLE sale_items_old;
        DROP TABLE sales_old;
        CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at);
        CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
        COMMIT;
    """)
    connection.execute("PRAGMA foreign_keys = ON")

def migrate_credit_payment_method(connection):
    """Allow the new credit wallet payment method on existing SQLite databases."""
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sales'").fetchone()
    sql = (row[0] or "") if row else ""
    if not row or "'Créditos'" in sql:
        return
    columns = {item[1] for item in connection.execute("PRAGMA table_info(sales)").fetchall()}
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("ALTER TABLE sales RENAME TO sales_credit_old")
    connection.executescript("""
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL REFERENCES players(id),
            payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia','Créditos')),
            total_cents INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 1,
            payment_status TEXT NOT NULL DEFAULT 'approved',
            mercadopago_order_id TEXT,
            mercadopago_payment_id TEXT,
            external_reference TEXT,
            idempotency_key TEXT,
            paid_at TEXT,
            ready_for_delivery INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT,
            delivered_by INTEGER REFERENCES users(id),
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            receipt_sent_at TEXT,
            receipt_error TEXT DEFAULT ''
        );
    """)
    target = ["id","player_id","payment_method","total_cents","paid","payment_status","mercadopago_order_id","mercadopago_payment_id","external_reference","idempotency_key","paid_at","ready_for_delivery","delivered_at","delivered_by","notes","created_at","receipt_sent_at","receipt_error"]
    source = [name if name in columns else "NULL" for name in target]
    connection.execute(f"INSERT INTO sales({','.join(target)}) SELECT {','.join(source)} FROM sales_credit_old")
    connection.execute("DROP TABLE sales_credit_old")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_sales_created ON sales(created_at)")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.commit()

def migrate_user_roles(connection):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or "'football_manager'" in (row[0] or ""):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript("""
        BEGIN;
        CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            password_required INTEGER NOT NULL DEFAULT 1,
            role TEXT NOT NULL CHECK(role IN ('manager','staff','client','infra','maintenance','display','football_manager')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users_new(id,username,name,password_hash,password_required,role,active,created_at)
        SELECT id,username,name,password_hash,password_required,role,active,created_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
        COMMIT;
    """)
    connection.execute("PRAGMA foreign_keys = ON")

def migrate_product_categories(connection):
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='products'"
    ).fetchone()
    if not row or "CHECK(category IN" not in (row[0] or ""):
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript("""
        BEGIN;
        ALTER TABLE sale_items RENAME TO sale_items_category_old;
        ALTER TABLE restocks RENAME TO restocks_category_old;
        ALTER TABLE stock_alert_states RENAME TO stock_alert_states_category_old;
        ALTER TABLE products RENAME TO products_category_old;
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            package_type TEXT NOT NULL DEFAULT '',
            units_per_case INTEGER NOT NULL DEFAULT 0 CHECK(units_per_case >= 0),
            price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
            cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
            stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
            min_stock INTEGER NOT NULL DEFAULT 5 CHECK(min_stock >= 0),
            supplier_email TEXT DEFAULT '',
            photo_data TEXT DEFAULT '',
            thumbnail_data TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price_cents INTEGER NOT NULL,
            unit_cost_cents INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE restocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_cost_cents INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE stock_alert_states (
            product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
            alerted INTEGER NOT NULL DEFAULT 0,
            last_stock INTEGER NOT NULL DEFAULT 0,
            last_notified_at TEXT
        );
        INSERT INTO products(id,name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,supplier_email,photo_data,thumbnail_data,active,created_at)
        SELECT id,name,category,package_type,units_per_case,price_cents,cost_cents,stock,min_stock,
               COALESCE(supplier_email,''),COALESCE(photo_data,''),COALESCE(thumbnail_data,''),active,created_at
        FROM products_category_old;
        INSERT INTO sale_items SELECT * FROM sale_items_category_old;
        INSERT INTO restocks SELECT * FROM restocks_category_old;
        INSERT INTO stock_alert_states SELECT * FROM stock_alert_states_category_old;
        DROP TABLE sale_items_category_old;
        DROP TABLE restocks_category_old;
        DROP TABLE stock_alert_states_category_old;
        DROP TABLE products_category_old;
        CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
        COMMIT;
    """)
    connection.execute("PRAGMA foreign_keys = ON")


def migrate_maintenance_areas(connection):
    """Allow the external area in databases created before that option existed."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='maintenance_requests'"
    ).fetchone()
    sql = (row[0] or "") if row else ""
    if row and "'EXT'" in sql and "'cancelled'" in sql:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.executescript("""
        BEGIN;
        ALTER TABLE maintenance_photos RENAME TO maintenance_photos_area_old;
        ALTER TABLE maintenance_requests RENAME TO maintenance_requests_area_old;
        CREATE TABLE maintenance_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            area_code TEXT NOT NULL CHECK(area_code IN ('BAR','COZ','SAL','HIS','VES','BAN','EXT')),
            location TEXT DEFAULT '',
            category TEXT NOT NULL CHECK(category IN ('electrical','plumbing','civil','painting','equipment','cleaning','other')),
            priority TEXT NOT NULL CHECK(priority IN ('low','medium','high','urgent')),
            description TEXT NOT NULL,
            responsible TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','analysis','in_progress','waiting_material','completed','cancelled')),
            occurred_on TEXT NOT NULL,
            due_on TEXT,
            resolution TEXT DEFAULT '',
            completed_on TEXT,
            cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
            notes TEXT DEFAULT '',
            created_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO maintenance_requests
        SELECT * FROM maintenance_requests_area_old;
        CREATE TABLE maintenance_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES maintenance_requests(id) ON DELETE CASCADE,
            phase TEXT NOT NULL CHECK(phase IN ('problem','resolution')),
            photo_data TEXT NOT NULL,
            thumbnail_data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO maintenance_photos
        SELECT * FROM maintenance_photos_area_old;
        DROP TABLE maintenance_photos_area_old;
        DROP TABLE maintenance_requests_area_old;
        CREATE INDEX IF NOT EXISTS idx_maintenance_status ON maintenance_requests(status);
        CREATE INDEX IF NOT EXISTS idx_maintenance_area ON maintenance_requests(area_code);
        CREATE INDEX IF NOT EXISTS idx_maintenance_photos_request ON maintenance_photos(request_id);
        CREATE TABLE IF NOT EXISTS maintenance_request_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES maintenance_requests(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            responsible TEXT DEFAULT '',
            observation TEXT DEFAULT '',
            changed_by INTEGER REFERENCES users(id),
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_maintenance_history_request ON maintenance_request_history(request_id, changed_at);
        COMMIT;
    """)
    connection.execute("PRAGMA foreign_keys = ON")

def init_sqlite(wrapper):
    conn = wrapper.conn
    migrate_user_roles(conn)
    migrate_payment_method(conn)
    migrate_credit_payment_method(conn)
    conn.executescript(SCHEMA)
    topup_columns = {row[1] for row in conn.execute("PRAGMA table_info(bar_credit_topups)")}
    if "refunded_at" not in topup_columns:
        conn.execute("ALTER TABLE bar_credit_topups ADD COLUMN refunded_at TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bar_credit_topups_idempotency ON bar_credit_topups(player_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bar_credit_audit_player ON bar_credit_audit(player_id,created_at)")
    conn.commit()
    migrate_maintenance_areas(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS maintenance_request_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES maintenance_requests(id) ON DELETE CASCADE,
        status TEXT NOT NULL, responsible TEXT DEFAULT '', observation TEXT DEFAULT '',
        changed_by INTEGER REFERENCES users(id), changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_history_request ON maintenance_request_history(request_id, changed_at)")
    conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
    if "email" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN email TEXT DEFAULT ''")
        conn.commit()
    if "membership_type" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN membership_type TEXT NOT NULL DEFAULT 'regular'")
        conn.commit()
    if "war_name" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN war_name TEXT DEFAULT ''")
    if "emergency_phone" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN emergency_phone TEXT DEFAULT ''")
    if "gender" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN gender TEXT NOT NULL DEFAULT 'male'")
    for column in ("birth_date", "postal_code", "address_street", "address_number", "address_complement", "address_neighborhood", "address_city", "address_state"):
        if column not in columns:
            conn.execute(f"ALTER TABLE players ADD COLUMN {column} TEXT DEFAULT ''")
    if "cpf" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN cpf TEXT DEFAULT ''")
    if "photo_data" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN photo_data TEXT DEFAULT ''")
    if "thumbnail_data" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN thumbnail_data TEXT DEFAULT ''")
    if "football_position" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN football_position TEXT DEFAULT ''")
    if "football_join_date" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN football_join_date TEXT DEFAULT ''")
    if "club_qr_data" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN club_qr_data TEXT DEFAULT ''")
    if "club_qr_token" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN club_qr_token TEXT DEFAULT ''")
    if "club_qr_updated_at" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN club_qr_updated_at TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_players_club_qr_token ON players(club_qr_token) WHERE club_qr_token<>''")
    conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens(user_id,used_at,expires_at)")
    conn.commit()

    reminder_columns = {row[1] for row in conn.execute("PRAGMA table_info(reminder_settings)")}
    if "push_enabled" not in reminder_columns:
        conn.execute("ALTER TABLE reminder_settings ADD COLUMN push_enabled INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    push_inbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(push_inbox)")}
    if "image_url" not in push_inbox_columns:
        conn.execute("ALTER TABLE push_inbox ADD COLUMN image_url TEXT DEFAULT ''")
        conn.commit()

    announcement_columns = {row[1] for row in conn.execute("PRAGMA table_info(push_announcements)")}
    if "status" not in announcement_columns:
        conn.execute("ALTER TABLE push_announcements ADD COLUMN status TEXT NOT NULL DEFAULT 'ENVIADO'")
        conn.commit()

    subscription_columns = {row[1] for row in conn.execute("PRAGMA table_info(push_subscriptions)")}
    for column, definition in (
        ("last_push_at", "TEXT"),
        ("last_push_status", "TEXT NOT NULL DEFAULT 'never'"),
        ("last_push_error", "TEXT DEFAULT ''"),
    ):
        if column not in subscription_columns:
            conn.execute(f"ALTER TABLE push_subscriptions ADD COLUMN {column} {definition}")
    if "body_html" not in push_inbox_columns:
        conn.execute("ALTER TABLE push_inbox ADD COLUMN body_html TEXT DEFAULT ''")
    product_columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "expiry_date" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN expiry_date TEXT DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS tribute_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
        title TEXT NOT NULL DEFAULT 'PELADEIROS GPCTA', body TEXT NOT NULL DEFAULT '🗣️ VEEENHAAAMMM...',
        body_html TEXT NOT NULL DEFAULT '🗣️ VEEENHAAAMMM...', image_data TEXT NOT NULL DEFAULT '',
        updated_by INTEGER REFERENCES users(id), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT OR IGNORE INTO tribute_settings(id) VALUES(1)")
    conn.execute("""CREATE TABLE IF NOT EXISTS tribute_schedules (
        weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6), enabled INTEGER NOT NULL DEFAULT 0,
        hour INTEGER NOT NULL DEFAULT 12 CHECK(hour BETWEEN 0 AND 23), updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT OR IGNORE INTO tribute_schedules(weekday,enabled,hour) VALUES(2,1,17)")
    conn.execute("INSERT OR IGNORE INTO tribute_schedules(weekday,enabled,hour) VALUES(5,1,15)")
    conn.commit()

    player_columns = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
    if "historical_only" not in player_columns:
        conn.execute("ALTER TABLE players ADD COLUMN historical_only INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    transfer_columns = {row[1] for row in conn.execute("PRAGMA table_info(football_transfer_requests)")}
    if not transfer_columns:
        conn.execute("""CREATE TABLE IF NOT EXISTS football_transfer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER NOT NULL REFERENCES players(id),
            window_year INTEGER NOT NULL, current_position TEXT NOT NULL DEFAULT '', requested_position TEXT NOT NULL,
            reason TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK(status IN ('PENDENTE','APROVADA','RECUSADA')),
            reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, review_notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(player_id, window_year))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_football_transfer_status ON football_transfer_requests(status,window_year)")
        conn.commit()

    conn.execute("""CREATE TABLE IF NOT EXISTS football_transfer_window_settings (
        id INTEGER PRIMARY KEY CHECK(id = 1),
        is_open INTEGER NOT NULL DEFAULT 0,
        manual_override INTEGER NOT NULL DEFAULT 0,
        window_year INTEGER,
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()

    incident_columns = {row[1] for row in conn.execute("PRAGMA table_info(football_incidents)")}
    if "card" not in incident_columns:
        conn.execute("ALTER TABLE football_incidents ADD COLUMN card TEXT DEFAULT ''")
        conn.commit()
    responsible_columns = {row[1] for row in conn.execute("PRAGMA table_info(football_responsibles)")}
    if "match_id" not in responsible_columns:
        conn.execute("ALTER TABLE football_responsibles ADD COLUMN match_id INTEGER REFERENCES football_matches(id) ON DELETE SET NULL")
        conn.commit()
    lineup_columns = {row[1] for row in conn.execute("PRAGMA table_info(football_lineups)")}
    if "period" not in lineup_columns:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript("""
            BEGIN;
            ALTER TABLE football_lineups RENAME TO football_lineups_legacy;
            CREATE TABLE football_lineups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
                player_id INTEGER NOT NULL REFERENCES players(id),
                team TEXT NOT NULL CHECK(team IN ('AZUL','BRANCO')),
                position TEXT NOT NULL CHECK(position IN ('GOLEIRO','DEFENSOR','MEIO_CAMPO','ATACANTE')),
                slot TEXT DEFAULT '',
                titular INTEGER NOT NULL DEFAULT 1,
                draw_order INTEGER,
                observation TEXT DEFAULT '',
                period INTEGER NOT NULL DEFAULT 1 CHECK(period IN (1,2)),
                UNIQUE(match_id, player_id, period)
            );
            INSERT INTO football_lineups(id,match_id,player_id,team,position,slot,titular,draw_order,observation,period)
                SELECT id,match_id,player_id,team,position,slot,titular,draw_order,observation,1
                FROM football_lineups_legacy;
            DROP TABLE football_lineups_legacy;
            COMMIT;
        """)
        conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_players_cpf ON players(cpf) WHERE cpf<>''")
    conn.commit()
    product_columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "package_type" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN package_type TEXT NOT NULL DEFAULT ''")
    if "units_per_case" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN units_per_case INTEGER NOT NULL DEFAULT 0")
    if "supplier_email" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN supplier_email TEXT DEFAULT ''")
    if "photo_data" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN photo_data TEXT DEFAULT ''")
    if "thumbnail_data" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN thumbnail_data TEXT DEFAULT ''")
    conn.commit()
    migrate_product_categories(conn)

    restock_item_columns = {row[1] for row in conn.execute("PRAGMA table_info(bar_restock_request_items)")}
    if "description" not in restock_item_columns:
        conn.execute("ALTER TABLE bar_restock_request_items ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        conn.commit()

    restock_request_columns = {row[1] for row in conn.execute("PRAGMA table_info(bar_restock_requests)")}
    if "workflow_status" not in restock_request_columns:
        conn.execute("ALTER TABLE bar_restock_requests ADD COLUMN workflow_status TEXT NOT NULL DEFAULT 'PENDENTE'")
        conn.execute("UPDATE bar_restock_requests SET workflow_status=status WHERE workflow_status='PENDENTE' AND status<>'PENDENTE'")
        conn.commit()
    for column, definition in (
        ("supplier", "TEXT NOT NULL DEFAULT ''"),
        ("purchase_amount_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("payment_account", "TEXT NOT NULL DEFAULT 'bank'"),
        ("receipt_data", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_filename", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_mime", "TEXT NOT NULL DEFAULT ''"),
        ("purchase_recorded_at", "TEXT"),
        ("purchase_recorded_by", "INTEGER REFERENCES users(id)"),
    ):
        if column not in restock_request_columns:
            conn.execute(f"ALTER TABLE bar_restock_requests ADD COLUMN {column} {definition}")
    conn.commit()
    conn.execute("""CREATE TABLE IF NOT EXISTS bar_restock_request_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
        status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', changed_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bar_restock_history_request ON bar_restock_request_history(request_id,created_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS bar_restock_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL, body TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bar_restock_notifications_user ON bar_restock_notifications(user_id,read_at,created_at)")
    conn.commit()
    
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "password_required" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_required INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    if "player_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN player_id INTEGER REFERENCES players(id)")
        conn.commit()
    conn.execute("""UPDATE users SET player_id=(
        SELECT p.id FROM players p WHERE p.active=1 AND p.war_name<>'' AND LOWER(p.war_name)=LOWER(users.username)
    ) WHERE role='client' AND player_id IS NULL""")
    conn.commit()

    sale_columns = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    # Eventos permitem vendas para convidados sem criar um cadastro de peladeiro.
    # Bancos SQLite antigos tinham player_id NOT NULL; reconstruímos a tabela uma
    # única vez para tornar a coluna opcional, preservando todos os registros.
    sale_info = list(conn.execute("PRAGMA table_info(sales)"))
    player_not_null = any(row[1] == "player_id" and int(row[3] or 0) == 1 for row in sale_info)
    if player_not_null:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE sales RENAME TO sales_legacy_event_migration")
        conn.execute("""CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER REFERENCES players(id),
            event_id INTEGER REFERENCES bar_events(id),
            guest_name TEXT NOT NULL DEFAULT '',
            payment_method TEXT NOT NULL CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia','Créditos')),
            total_cents INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 1,
            payment_status TEXT NOT NULL DEFAULT 'approved',
            mercadopago_order_id TEXT, mercadopago_payment_id TEXT,
            external_reference TEXT, idempotency_key TEXT, paid_at TEXT,
            ready_for_delivery INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT, delivered_by INTEGER REFERENCES users(id),
            notes TEXT DEFAULT '', receipt_sent_at TEXT, receipt_error TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        old_columns = {row[1] for row in conn.execute("PRAGMA table_info(sales_legacy_event_migration)")}
        target_columns = ["id", "player_id", "payment_method", "total_cents", "paid", "payment_status",
                          "mercadopago_order_id", "mercadopago_payment_id", "external_reference", "idempotency_key",
                          "paid_at", "ready_for_delivery", "delivered_at", "delivered_by", "notes", "receipt_sent_at",
                          "receipt_error", "created_at"]
        available = [column for column in target_columns if column in old_columns]
        conn.execute(f"INSERT INTO sales({','.join(available)}) SELECT {','.join(available)} FROM sales_legacy_event_migration")
        conn.execute("DROP TABLE sales_legacy_event_migration")
        conn.execute("PRAGMA foreign_keys = ON")
        sale_columns = {row[1] for row in conn.execute("PRAGMA table_info(sales)")}
    for column, definition in {
        "event_id": "INTEGER REFERENCES bar_events(id)",
        "guest_name": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in sale_columns:
            conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_event ON sales(event_id,created_at)")
    sale_migrations = {
        "payment_status": "TEXT NOT NULL DEFAULT 'approved'",
        "mercadopago_order_id": "TEXT",
        "mercadopago_payment_id": "TEXT",
        "external_reference": "TEXT",
        "idempotency_key": "TEXT",
        "paid_at": "TEXT",
        "ready_for_delivery": "INTEGER NOT NULL DEFAULT 0",
        "delivered_at": "TEXT",
        "delivered_by": "INTEGER REFERENCES users(id)",
        "receipt_sent_at": "TEXT",
        "receipt_error": "TEXT DEFAULT ''",
    }
    for column, definition in sale_migrations.items():
        if column not in sale_columns:
            conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_mp_order ON sales(mercadopago_order_id) WHERE mercadopago_order_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_external_reference ON sales(external_reference) WHERE external_reference IS NOT NULL")
    conn.commit()

    sumula_columns = {row[1] for row in conn.execute("PRAGMA table_info(football_sumulas)")}
    for column, definition in {
        "locked_at": "TEXT",
        "locked_by": "INTEGER REFERENCES users(id)",
    }.items():
        if column not in sumula_columns:
            conn.execute(f"ALTER TABLE football_sumulas ADD COLUMN {column} {definition}")
    conn.commit()

    goal_columns = {row[1] for row in conn.execute("PRAGMA table_info(football_goals)")}
    if "own_goal" not in goal_columns:
        conn.execute("ALTER TABLE football_goals ADD COLUMN own_goal INTEGER NOT NULL DEFAULT 0")
    if "goal_type" not in goal_columns:
        conn.execute("ALTER TABLE football_goals ADD COLUMN goal_type TEXT NOT NULL DEFAULT 'NORMAL'")
    conn.execute("UPDATE football_goals SET goal_type='CONTRA' WHERE COALESCE(own_goal,0)=1")
    conn.execute("UPDATE football_goals SET own_goal=1 WHERE goal_type='CONTRA'")
    conn.execute("UPDATE football_goals SET assist_player_id=NULL WHERE goal_type!='NORMAL' OR own_goal=1")
    conn.commit()

    load_columns = {row[1] for row in conn.execute("PRAGMA table_info(load_entries)")}
    # Expand the original two-state status constraint without losing existing
    # loads or their photos.  SQLite cannot alter a CHECK constraint in place.
    load_schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='load_entries'").fetchone()
    load_schema_sql = (load_schema[0] if load_schema else "") or ""
    if "maintenance" not in load_schema_sql:
        conn.execute("PRAGMA foreign_keys=OFF")
        # Renaming a referenced table causes SQLite to rewrite foreign keys in
        # dependent tables to the temporary name. Rebuild the photo table too,
        # otherwise a later upload could reference the dropped legacy table.
        photos_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='load_entry_photos'"
        ).fetchone() is not None
        if photos_exists:
            conn.execute("ALTER TABLE load_entry_photos RENAME TO load_entry_photos_legacy")
        conn.execute("ALTER TABLE load_entries RENAME TO load_entries_legacy")
        conn.execute("""CREATE TABLE load_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL REFERENCES materials(id),
            bmp TEXT NOT NULL UNIQUE,
            area_code TEXT NOT NULL DEFAULT 'BAR' CHECK(area_code IN ('BAR','COZ','SAL','HIS','VES','BAN')),
            serial_number TEXT DEFAULT '', location TEXT DEFAULT '', responsible TEXT DEFAULT '', notes TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','maintenance','discharged','lost','borrowed')),
            discharged_at TEXT, discharged_by INTEGER REFERENCES users(id),
            last_checked_at TEXT, last_checked_by INTEGER REFERENCES users(id), next_check_due_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(load_entries_legacy)")}
        responsible_expr = "responsible" if "responsible" in legacy_columns else "''"
        conn.execute(f"""INSERT INTO load_entries
            (id,material_id,bmp,area_code,serial_number,location,responsible,notes,status,discharged_at,discharged_by,
             last_checked_at,last_checked_by,next_check_due_at,created_at,updated_at)
            SELECT id,material_id,bmp,area_code,serial_number,location,{responsible_expr},notes,status,discharged_at,discharged_by,
                   last_checked_at,last_checked_by,next_check_due_at,created_at,updated_at
            FROM load_entries_legacy""")
        conn.execute("DROP TABLE load_entries_legacy")
        if photos_exists:
            conn.execute("""CREATE TABLE load_entry_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                load_entry_id INTEGER NOT NULL REFERENCES load_entries(id) ON DELETE CASCADE,
                photo_data TEXT NOT NULL,
                thumbnail_data TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""INSERT INTO load_entry_photos(id,load_entry_id,photo_data,thumbnail_data,created_at)
                SELECT id,load_entry_id,photo_data,thumbnail_data,created_at
                FROM load_entry_photos_legacy""")
            conn.execute("DROP TABLE load_entry_photos_legacy")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_load_photos_entry ON load_entry_photos(load_entry_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_load_entries_material ON load_entries(material_id)")
        conn.execute("PRAGMA foreign_keys=ON")
        load_columns = {row[1] for row in conn.execute("PRAGMA table_info(load_entries)")}
    load_migrations = {
        "area_code": "TEXT NOT NULL DEFAULT 'BAR' CHECK(area_code IN ('BAR','COZ','SAL','HIS','VES','BAN'))",
        "status": "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','maintenance','discharged','lost','borrowed'))",
        "discharged_at": "TEXT",
        "discharged_by": "INTEGER REFERENCES users(id)",
        "last_checked_at": "TEXT",
        "last_checked_by": "INTEGER REFERENCES users(id)",
        "next_check_due_at": "TEXT",
        "responsible": "TEXT DEFAULT ''",
    }
    for column, definition in load_migrations.items():
        if column not in load_columns:
            conn.execute(f"ALTER TABLE load_entries ADD COLUMN {column} {definition}")
    photo_columns = {row[1] for row in conn.execute("PRAGMA table_info(load_entry_photos)")}
    photo_migrations = {
        "photo_kind": "TEXT NOT NULL DEFAULT 'registration'",
        # SQLite does not accept CURRENT_TIMESTAMP as the default while adding
        # a column to an existing table. New rows set this value explicitly.
        "captured_at": "TEXT",
        "captured_by": "INTEGER REFERENCES users(id)",
    }
    for column, definition in photo_migrations.items():
        if column not in photo_columns:
            conn.execute(f"ALTER TABLE load_entry_photos ADD COLUMN {column} {definition}")
    conn.execute("UPDATE load_entry_photos SET captured_at=COALESCE(captured_at,created_at,CURRENT_TIMESTAMP)")
    conn.execute("UPDATE load_entries SET bmp=bmp || ' | BAR' WHERE bmp NOT LIKE '%|%'")
    conn.execute("""CREATE TABLE IF NOT EXISTS load_entry_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        load_entry_id INTEGER NOT NULL REFERENCES load_entries(id) ON DELETE CASCADE,
        from_location TEXT DEFAULT '', to_location TEXT DEFAULT '',
        from_responsible TEXT DEFAULT '', to_responsible TEXT DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', moved_by INTEGER REFERENCES users(id),
        moved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_load_movements_entry ON load_entry_movements(load_entry_id,moved_at)")
    conn.commit()

def init_postgres(wrapper):
    wrapper.execute("""
    CREATE OR REPLACE FUNCTION date(t timestamp with time zone) RETURNS date AS $$
        SELECT timezone('America/Sao_Paulo', t)::date;
    $$ LANGUAGE SQL IMMUTABLE;
    """)
    wrapper.execute("""
    CREATE OR REPLACE FUNCTION date(t timestamp without time zone) RETURNS date AS $$
        SELECT timezone('America/Sao_Paulo', t AT TIME ZONE 'UTC')::date;
    $$ LANGUAGE SQL IMMUTABLE;
    """)
    wrapper.execute("""
    CREATE OR REPLACE FUNCTION date(t text) RETURNS date AS $$
        SELECT timezone('America/Sao_Paulo', t::timestamp AT TIME ZONE 'UTC')::date;
    $$ LANGUAGE SQL IMMUTABLE;
    """)
    
    pg_schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    pg_schema = pg_schema.replace("COLLATE NOCASE", "")
    pg_schema = pg_schema.replace("created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP", "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
    pg_schema = pg_schema.replace("paid_at TEXT", "paid_at TIMESTAMP")
    pg_schema = pg_schema.replace("delivered_at TEXT", "delivered_at TIMESTAMP")
    pg_schema = pg_schema.replace("discharged_at TEXT", "discharged_at TIMESTAMP")
    
    for stmt in pg_schema.split(';'):
        stmt_clean = stmt.strip()
        # SQLite seed statements are reapplied below with PostgreSQL's
        # idempotent ON CONFLICT syntax after their tables exist.
        if stmt_clean.upper().startswith("INSERT OR IGNORE"):
            continue
        if stmt_clean:
            wrapper.execute(stmt_clean)
    
    # Run migration to add password_required if not exists in postgres
    wrapper.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_required INTEGER NOT NULL DEFAULT 1")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE, expires_at TIMESTAMP NOT NULL, used_at TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens(user_id,used_at,expires_at)")
    wrapper.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS player_id INTEGER REFERENCES players(id)")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS photo_data TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS thumbnail_data TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS football_position TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS football_join_date TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS club_qr_data TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS club_qr_token TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS club_qr_updated_at TEXT")
    wrapper.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_players_club_qr_token ON players(club_qr_token) WHERE club_qr_token<>''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS gender TEXT NOT NULL DEFAULT 'male'")
    wrapper.execute("ALTER TABLE reminder_settings ADD COLUMN IF NOT EXISTS push_enabled INTEGER NOT NULL DEFAULT 1")
    wrapper.execute("ALTER TABLE push_inbox ADD COLUMN IF NOT EXISTS image_url TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE push_announcements ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ENVIADO'")
    wrapper.execute("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS last_push_at TIMESTAMP")
    wrapper.execute("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS last_push_status TEXT NOT NULL DEFAULT 'never'")
    wrapper.execute("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS last_push_error TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE push_inbox ADD COLUMN IF NOT EXISTS body_html TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS historical_only INTEGER NOT NULL DEFAULT 0")
    wrapper.execute("ALTER TABLE football_goals ADD COLUMN IF NOT EXISTS own_goal INTEGER NOT NULL DEFAULT 0")
    wrapper.execute("ALTER TABLE football_goals ADD COLUMN IF NOT EXISTS goal_type TEXT NOT NULL DEFAULT 'NORMAL'")
    wrapper.execute("UPDATE football_goals SET goal_type='CONTRA' WHERE COALESCE(own_goal,0)=1 AND COALESCE(goal_type,'NORMAL')='NORMAL'")
    wrapper.execute("UPDATE football_goals SET own_goal=1 WHERE goal_type='CONTRA'")
    wrapper.execute("UPDATE football_goals SET assist_player_id=NULL WHERE goal_type!='NORMAL' OR own_goal=1")
    wrapper.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS supplier_email TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_data TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS thumbnail_data TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS expiry_date TEXT DEFAULT ''")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS tribute_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 1,
        title TEXT NOT NULL DEFAULT 'PELADEIROS GPCTA', body TEXT NOT NULL DEFAULT '🗣️ VEEENHAAAMMM...',
        body_html TEXT NOT NULL DEFAULT '🗣️ VEEENHAAAMMM...', image_data TEXT NOT NULL DEFAULT '',
        updated_by INTEGER REFERENCES users(id), updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("INSERT INTO tribute_settings(id) VALUES(1) ON CONFLICT(id) DO NOTHING")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS tribute_schedules (
        weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6), enabled INTEGER NOT NULL DEFAULT 0,
        hour INTEGER NOT NULL DEFAULT 12 CHECK(hour BETWEEN 0 AND 23), updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("INSERT INTO tribute_schedules(weekday,enabled,hour) VALUES(2,1,17) ON CONFLICT(weekday) DO NOTHING RETURNING weekday")
    wrapper.execute("INSERT INTO tribute_schedules(weekday,enabled,hour) VALUES(5,1,15) ON CONFLICT(weekday) DO NOTHING RETURNING weekday")
    wrapper.execute("ALTER TABLE bar_restock_request_items ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS workflow_status TEXT NOT NULL DEFAULT 'PENDENTE'")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS supplier TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS purchase_amount_cents INTEGER NOT NULL DEFAULT 0")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS payment_account TEXT NOT NULL DEFAULT 'bank'")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS receipt_data TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS receipt_filename TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS receipt_mime TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS purchase_recorded_at TIMESTAMP")
    wrapper.execute("ALTER TABLE bar_restock_requests ADD COLUMN IF NOT EXISTS purchase_recorded_by INTEGER REFERENCES users(id)")
    wrapper.execute("UPDATE bar_restock_requests SET workflow_status=status WHERE workflow_status='PENDENTE' AND status<>'PENDENTE'")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS bar_restock_request_history (
        id SERIAL PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
        status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', changed_by INTEGER NOT NULL REFERENCES users(id),
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_bar_restock_history_request ON bar_restock_request_history(request_id,created_at)")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS bar_restock_notifications (
        id SERIAL PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES bar_restock_requests(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL, body TEXT NOT NULL, read_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_bar_restock_notifications_user ON bar_restock_notifications(user_id,read_at,created_at)")
    for column in ("birth_date", "postal_code", "address_street", "address_number", "address_complement", "address_neighborhood", "address_city", "address_state"):
        wrapper.execute(f"ALTER TABLE players ADD COLUMN IF NOT EXISTS {column} TEXT DEFAULT ''")
    wrapper.execute("""UPDATE users SET player_id=(
        SELECT p.id FROM players p WHERE p.active=1 AND p.war_name<>'' AND LOWER(p.war_name)=LOWER(users.username)
    ) WHERE role='client' AND player_id IS NULL""")
    wrapper.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    wrapper.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK(role IN ('manager','staff','client','infra','maintenance','display','football_manager'))")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'approved'")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS mercadopago_order_id TEXT")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS mercadopago_payment_id TEXT")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS external_reference TEXT")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS idempotency_key TEXT")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS ready_for_delivery INTEGER NOT NULL DEFAULT 0")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS delivered_by INTEGER REFERENCES users(id)")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS receipt_sent_at TIMESTAMP")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS receipt_error TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS event_id INTEGER REFERENCES bar_events(id)")
    wrapper.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS guest_name TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE sales ALTER COLUMN player_id DROP NOT NULL")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_sales_event ON sales(event_id,created_at)")
    wrapper.execute("ALTER TABLE sales DROP CONSTRAINT IF EXISTS sales_payment_method_check")
    wrapper.execute("ALTER TABLE sales ADD CONSTRAINT sales_payment_method_check CHECK(payment_method IN ('Pix','Dinheiro','Débito','Cortesia','Créditos'))")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS bar_credit_accounts (
        id SERIAL PRIMARY KEY, player_id INTEGER NOT NULL UNIQUE REFERENCES players(id) ON DELETE CASCADE,
        balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0), low_balance_notified INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS bar_credit_topups (
        id SERIAL PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0), payment_method TEXT NOT NULL DEFAULT 'Pix',
        paid INTEGER NOT NULL DEFAULT 0, payment_status TEXT NOT NULL DEFAULT 'creating',
        mercadopago_order_id TEXT, mercadopago_payment_id TEXT, external_reference TEXT UNIQUE,
        idempotency_key TEXT, paid_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS bar_credit_transactions (
        id SERIAL PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        type TEXT NOT NULL CHECK(type IN ('PURCHASE','CONSUMPTION','ADJUSTMENT','REFUND')),
        amount_cents INTEGER NOT NULL, balance_after_cents INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
        sale_id INTEGER REFERENCES sales(id) ON DELETE SET NULL, topup_id INTEGER REFERENCES bar_credit_topups(id) ON DELETE SET NULL,
        created_by INTEGER REFERENCES users(id), created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_bar_credit_transactions_player ON bar_credit_transactions(player_id,created_at)")
    wrapper.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bar_credit_topups_mp_order ON bar_credit_topups(mercadopago_order_id) WHERE mercadopago_order_id IS NOT NULL")
    wrapper.execute("ALTER TABLE bar_credit_topups ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMP")
    wrapper.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bar_credit_topups_idempotency ON bar_credit_topups(player_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS bar_credit_audit (
        id SERIAL PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
        action TEXT NOT NULL, amount_cents INTEGER NOT NULL DEFAULT 0,
        topup_id INTEGER REFERENCES bar_credit_topups(id) ON DELETE SET NULL,
        transaction_id INTEGER REFERENCES bar_credit_transactions(id) ON DELETE SET NULL,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        reason TEXT NOT NULL DEFAULT '', created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_bar_credit_audit_player ON bar_credit_audit(player_id,created_at)")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS sale_item_deliveries (
        id SERIAL PRIMARY KEY,
        sale_item_id INTEGER NOT NULL REFERENCES sale_items(id) ON DELETE CASCADE,
        quantity INTEGER NOT NULL CHECK(quantity > 0),
        delivered_by INTEGER REFERENCES users(id),
        delivered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_sale_item_deliveries_item ON sale_item_deliveries(sale_item_id)")
    wrapper.execute("ALTER TABLE football_sumulas ADD COLUMN IF NOT EXISTS locked_at TIMESTAMP")
    wrapper.execute("ALTER TABLE football_sumulas ADD COLUMN IF NOT EXISTS locked_by INTEGER REFERENCES users(id)")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS area_code TEXT NOT NULL DEFAULT 'BAR'")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS discharged_at TIMESTAMP")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS discharged_by INTEGER REFERENCES users(id)")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMP")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS last_checked_by INTEGER REFERENCES users(id)")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS next_check_due_at TIMESTAMP")
    wrapper.execute("ALTER TABLE load_entries ADD COLUMN IF NOT EXISTS responsible TEXT NOT NULL DEFAULT ''")
    wrapper.execute("ALTER TABLE load_entry_photos ADD COLUMN IF NOT EXISTS photo_kind TEXT NOT NULL DEFAULT 'registration'")
    wrapper.execute("ALTER TABLE load_entry_photos ADD COLUMN IF NOT EXISTS captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")
    wrapper.execute("ALTER TABLE load_entry_photos ADD COLUMN IF NOT EXISTS captured_by INTEGER REFERENCES users(id)")
    wrapper.execute("ALTER TABLE load_entries DROP CONSTRAINT IF EXISTS load_entries_status_check")
    wrapper.execute("""ALTER TABLE load_entries ADD CONSTRAINT load_entries_status_check
        CHECK(status IN ('active','maintenance','discharged','lost','borrowed'))""")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS load_entry_movements (
        id SERIAL PRIMARY KEY,
        load_entry_id INTEGER NOT NULL REFERENCES load_entries(id) ON DELETE CASCADE,
        from_location TEXT DEFAULT '', to_location TEXT DEFAULT '',
        from_responsible TEXT DEFAULT '', to_responsible TEXT DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', moved_by INTEGER REFERENCES users(id),
        moved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_load_movements_entry ON load_entry_movements(load_entry_id,moved_at)")
    wrapper.execute("ALTER TABLE football_incidents ADD COLUMN IF NOT EXISTS card TEXT DEFAULT ''")
    wrapper.execute("ALTER TABLE football_responsibles ADD COLUMN IF NOT EXISTS match_id INTEGER REFERENCES football_matches(id) ON DELETE SET NULL")
    wrapper.execute("ALTER TABLE football_lineups ADD COLUMN IF NOT EXISTS period INTEGER NOT NULL DEFAULT 1")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS football_participant_matches (
        id SERIAL PRIMARY KEY,
        sumula_id INTEGER NOT NULL REFERENCES football_sumulas(id) ON DELETE CASCADE,
        match_id INTEGER NOT NULL REFERENCES football_matches(id) ON DELETE CASCADE,
        player_id INTEGER NOT NULL REFERENCES players(id),
        status TEXT NOT NULL DEFAULT 'CONFIRMADO' CHECK(status IN ('CONFIRMADO','AUSENTE','DESISTENTE','RESERVA')),
        draw_order INTEGER,
        observation TEXT DEFAULT '',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(sumula_id, match_id, player_id)
    )""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_football_participant_matches_sumula ON football_participant_matches(sumula_id,match_id)")
    wrapper.execute("ALTER TABLE football_lineups DROP CONSTRAINT IF EXISTS football_lineups_match_id_player_id_key")
    wrapper.execute("""DO $$
    DECLARE item RECORD;
    BEGIN
        FOR item IN
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'football_lineups'
              AND c.contype = 'u'
              AND pg_get_constraintdef(c.oid) ILIKE '%(match_id, player_id)%'
        LOOP
            EXECUTE format('ALTER TABLE football_lineups DROP CONSTRAINT IF EXISTS %I', item.conname);
        END LOOP;
    END $$;""")
    wrapper.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_football_lineups_match_player_period ON football_lineups(match_id,player_id,period)")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS football_deleted_sumula_audit (
        id SERIAL PRIMARY KEY, sumula_id INTEGER NOT NULL, match_date DATE NOT NULL,
        day_pelada TEXT NOT NULL, local TEXT DEFAULT '', deleted_by INTEGER REFERENCES users(id),
        deleted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS football_transfer_requests (
        id SERIAL PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id), window_year INTEGER NOT NULL,
        current_position TEXT NOT NULL DEFAULT '', requested_position TEXT NOT NULL, reason TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK(status IN ('PENDENTE','APROVADA','RECUSADA')),
        reviewed_by INTEGER REFERENCES users(id), reviewed_at TIMESTAMP, review_notes TEXT DEFAULT '',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(player_id, window_year))""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_football_transfer_status ON football_transfer_requests(status,window_year)")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS football_transfer_window_settings (
        id SERIAL PRIMARY KEY CHECK(id = 1),
        is_open INTEGER NOT NULL DEFAULT 0,
        manual_override INTEGER NOT NULL DEFAULT 0,
        window_year INTEGER,
        updated_by INTEGER REFERENCES users(id),
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    wrapper.execute("ALTER TABLE maintenance_requests DROP CONSTRAINT IF EXISTS maintenance_requests_area_code_check")
    wrapper.execute("ALTER TABLE maintenance_requests ADD CONSTRAINT maintenance_requests_area_code_check CHECK(area_code IN ('BAR','COZ','SAL','HIS','VES','BAN','EXT'))")
    wrapper.execute("ALTER TABLE maintenance_requests DROP CONSTRAINT IF EXISTS maintenance_requests_status_check")
    wrapper.execute("ALTER TABLE maintenance_requests ADD CONSTRAINT maintenance_requests_status_check CHECK(status IN ('open','analysis','in_progress','waiting_material','completed','cancelled'))")
    wrapper.execute("""CREATE TABLE IF NOT EXISTS maintenance_request_history (
        id SERIAL PRIMARY KEY,
        request_id INTEGER NOT NULL REFERENCES maintenance_requests(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        responsible TEXT DEFAULT '',
        observation TEXT DEFAULT '',
        changed_by INTEGER REFERENCES users(id),
        changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    wrapper.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_history_request ON maintenance_request_history(request_id, changed_at)")
    wrapper.execute("UPDATE load_entries SET bmp=bmp || ' | BAR' WHERE bmp NOT LIKE '%|%'")
    wrapper.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_mp_order ON sales(mercadopago_order_id) WHERE mercadopago_order_id IS NOT NULL")
    wrapper.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_external_reference ON sales(external_reference) WHERE external_reference IS NOT NULL")
    wrapper.commit()
