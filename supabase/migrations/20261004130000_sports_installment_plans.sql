-- Phase 1: sports ready-stock installment modeling only; no external charges.
CREATE TABLE IF NOT EXISTS sports_installment_plans (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL UNIQUE REFERENCES sales(id) ON DELETE RESTRICT,
    total_cents INTEGER NOT NULL CHECK(total_cents >= 3),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','paid')),
    first_installment_paid_at TIMESTAMPTZ,
    fully_paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sports_installments (
    id BIGSERIAL PRIMARY KEY,
    plan_id BIGINT NOT NULL REFERENCES sports_installment_plans(id) ON DELETE RESTRICT,
    installment_number INTEGER NOT NULL CHECK(installment_number IN (1,2,3)),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    due_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')),
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id,installment_number)
);
CREATE INDEX IF NOT EXISTS idx_sports_installments_status_due
    ON sports_installments(status,due_date);
