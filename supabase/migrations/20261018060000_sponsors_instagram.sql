-- Additive: existing sponsor names, positions, images and sites are preserved.
-- Execute through GPCTA's transactional migration runner before deploying code.
SET LOCAL lock_timeout = '2000ms';
SET LOCAL statement_timeout = '30000ms';

ALTER TABLE sponsors
  ADD COLUMN IF NOT EXISTS instagram_username TEXT NOT NULL DEFAULT '';
