ALTER TABLE load_entries
    DROP CONSTRAINT IF EXISTS load_entries_area_code_check;

ALTER TABLE load_entries
    ADD CONSTRAINT load_entries_area_code_check
    CHECK (area_code IN ('BAR', 'COZ', 'SAL', 'HIS', 'VES', 'BAN', 'INT'));

ALTER TABLE maintenance_requests
    DROP CONSTRAINT IF EXISTS maintenance_requests_area_code_check;

ALTER TABLE maintenance_requests
    ADD CONSTRAINT maintenance_requests_area_code_check
    CHECK (area_code IN ('BAR', 'COZ', 'SAL', 'HIS', 'VES', 'BAN', 'INT', 'EXT'));
