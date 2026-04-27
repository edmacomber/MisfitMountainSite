-- Misfit Monsters / Misfit City — add optional player_location column.
-- Apply once, before the v1.3.0 app.py drops:
--   sudo -u www-data sqlite3 /var/www/mm-leaderboard/scores.db < mm_location_migration.sql
--
-- Back-compat:
--   • Nullable, so existing rows are valid (NULL = unknown).
--   • Old clients that don't send playerLocation keep inserting rows
--     with NULL location.
--   • New clients include the player's self-reported high-level
--     location string (e.g. "Maine, USA"). Server caps at 32 chars,
--     does the same name-style sanitization.

BEGIN;

ALTER TABLE scores ADD COLUMN player_location TEXT;

COMMIT;
