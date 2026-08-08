ALTER TABLE players ADD COLUMN IF NOT EXISTS club_qr_data TEXT DEFAULT '';
ALTER TABLE players ADD COLUMN IF NOT EXISTS club_qr_token TEXT DEFAULT '';
ALTER TABLE players ADD COLUMN IF NOT EXISTS club_qr_updated_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_players_club_qr_token
ON players(club_qr_token)
WHERE club_qr_token<>'';
