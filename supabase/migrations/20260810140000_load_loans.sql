CREATE TABLE IF NOT EXISTS load_loans (
    id SERIAL PRIMARY KEY, borrower_name TEXT NOT NULL, borrower_phone TEXT DEFAULT '',
    borrower_document TEXT DEFAULT '', checkout_on TEXT NOT NULL, due_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','partial','returned','cancelled')),
    notes TEXT DEFAULT '', departure_photo_data TEXT DEFAULT '', departure_thumbnail_data TEXT DEFAULT '',
    return_photo_data TEXT DEFAULT '', return_thumbnail_data TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id), returned_by INTEGER REFERENCES users(id), returned_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS load_loan_items (
    id SERIAL PRIMARY KEY, loan_id INTEGER NOT NULL REFERENCES load_loans(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES materials(id), quantity INTEGER NOT NULL CHECK(quantity>0),
    returned_quantity INTEGER NOT NULL DEFAULT 0 CHECK(returned_quantity>=0), UNIQUE(loan_id,material_id)
);
CREATE TABLE IF NOT EXISTS load_loan_history (
    id SERIAL PRIMARY KEY, loan_id INTEGER NOT NULL REFERENCES load_loans(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', changed_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_load_loans_status_due ON load_loans(status,due_on);
CREATE INDEX IF NOT EXISTS idx_load_loan_items_loan ON load_loan_items(loan_id);
CREATE INDEX IF NOT EXISTS idx_load_loan_history_loan ON load_loan_history(loan_id,created_at);
