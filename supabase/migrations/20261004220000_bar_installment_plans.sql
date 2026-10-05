-- Bar case-only checkout modeling. No payments, stock or delivery changes.
CREATE TABLE IF NOT EXISTS bar_installment_plans (
    id BIGSERIAL PRIMARY KEY,
    sale_id BIGINT NOT NULL UNIQUE REFERENCES sales(id) ON DELETE RESTRICT,
    total_cents INTEGER NOT NULL CHECK(total_cents >= 2),
    checkout_origin TEXT NOT NULL CHECK(checkout_origin = 'case_only'),
    checkout_snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','paid')),
    first_installment_paid_at TIMESTAMPTZ,
    fully_paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bar_installments (
    id BIGSERIAL PRIMARY KEY,
    plan_id BIGINT NOT NULL REFERENCES bar_installment_plans(id) ON DELETE RESTRICT,
    installment_number INTEGER NOT NULL CHECK(installment_number IN (1,2)),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    due_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid')),
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plan_id,installment_number)
);
CREATE INDEX IF NOT EXISTS idx_bar_installments_status_due ON bar_installments(status,due_date);
