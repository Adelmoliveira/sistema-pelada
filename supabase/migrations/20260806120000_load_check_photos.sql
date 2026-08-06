ALTER TABLE load_entry_photos
    ADD COLUMN IF NOT EXISTS photo_kind TEXT NOT NULL DEFAULT 'registration';

ALTER TABLE load_entry_photos
    ADD COLUMN IF NOT EXISTS captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE load_entry_photos
    ADD COLUMN IF NOT EXISTS captured_by INTEGER REFERENCES users(id);
