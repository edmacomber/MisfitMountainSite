-- Misfit Monsters / Misfit City — geocoding columns + cache table.
-- Apply once, before the v1.4.0-geocode app.py drops:
--   sudo -u www-data sqlite3 /var/www/mm-leaderboard/scores.db < mm_geocode_migration.sql
--
-- What it adds:
--   • scores.player_lat REAL / player_lng REAL (nullable). NULL means
--     either the player hasn't set a location yet OR the geocoder
--     hasn't resolved it yet — both render the same on clients (no
--     pin on the globe).
--   • geocode_cache (location_key TEXT PK, lat REAL, lng REAL,
--     resolved_at TEXT). Normalized-lowercase location strings map to
--     their Nominatim-resolved centroid. Keeps /submit fast (first
--     lookup for a new location hits Nominatim once; every other
--     player from that city/region gets the cached answer instantly).
--
-- Back-compat: old app.py versions just ignore the new columns and
-- geocode_cache table. New app.py writes them.

BEGIN;

ALTER TABLE scores ADD COLUMN player_lat REAL;
ALTER TABLE scores ADD COLUMN player_lng REAL;

CREATE TABLE IF NOT EXISTS geocode_cache (
  location_key  TEXT PRIMARY KEY,  -- lowercased + whitespace-normalized location string
  lat           REAL NOT NULL,
  lng           REAL NOT NULL,
  display_name  TEXT,              -- Nominatim's canonical name, purely informational
  resolved_at   TEXT NOT NULL
);

COMMIT;
