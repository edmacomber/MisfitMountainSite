-- Mountain Studio API schema (multi-game)
-- Managed by app.py via sqlite3 stdlib — no migrations framework.
-- `game_id` scopes every row to a single game in the portfolio so one
-- DB can host leaderboards for Misfit Monsters, Misfit City, etc. New
-- rows default to 'misfit-monsters' for back-compat with clients that
-- don't send gameId.
--
-- For an existing database that pre-dates the multi-game work, apply
-- the corresponding migration at server/../MisfitCity-Phaser/server/
-- mc_migration.sql (one-shot).

-- Per-player best score for each (game, level). Upserts on improvement.
-- `player_location` is an opt-in high-level string the player sets in
-- Settings (e.g. "Maine, USA"). Kept in sync across all of a player's
-- rows by /submit so the /top aggregate can MAX() it cheaply.
-- `player_lat` / `player_lng` are the Nominatim-resolved coordinates of
-- the location, populated asynchronously via a BackgroundTask in
-- /submit so the user's level-complete flow never waits on geocoding.
-- NULL lat/lng means either no location yet OR geocoding hasn't
-- landed — clients render both the same (no globe pin).
CREATE TABLE IF NOT EXISTS scores (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id         TEXT NOT NULL DEFAULT 'misfit-monsters',
  player_id       TEXT NOT NULL,
  player_name     TEXT NOT NULL,
  player_location TEXT,
  player_lat      REAL,
  player_lng      REAL,
  level_index     INTEGER NOT NULL,
  score           INTEGER NOT NULL CHECK(score BETWEEN 0 AND 9999999),
  stars           INTEGER NOT NULL CHECK(stars BETWEEN 0 AND 3),
  submitted_at    TEXT NOT NULL,
  client_ip       TEXT,
  UNIQUE(game_id, player_id, level_index)
);

-- Geocode cache — one row per unique normalized location string
-- (lowercase, whitespace-collapsed). Populated by app.py's background
-- geocode task. Every subsequent /submit from the same location hits
-- this cache instead of Nominatim, so the 1-req/sec rate limit is
-- never a practical concern at our scale.
CREATE TABLE IF NOT EXISTS geocode_cache (
  location_key  TEXT PRIMARY KEY,
  lat           REAL NOT NULL,
  lng           REAL NOT NULL,
  display_name  TEXT,
  resolved_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_level_score ON scores(game_id, level_index, score DESC);
CREATE INDEX IF NOT EXISTS idx_player_game      ON scores(player_id, game_id);

-- Every level-end event (complete / fail / quit). Append-only for
-- analytics: difficulty curve, engagement, booster usage, creature
-- popularity. Not rate-limited by uniqueness like scores — a player
-- can fail a level many times and we want each attempt logged.
-- `creature_used` is MM-specific (0-8 creature id); harmless NULL for
-- games that don't use it.
CREATE TABLE IF NOT EXISTS level_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id        TEXT NOT NULL DEFAULT 'misfit-monsters',
  player_id      TEXT NOT NULL,
  level_index    INTEGER NOT NULL,
  outcome        TEXT NOT NULL CHECK(outcome IN ('complete', 'fail', 'quit')),
  score          INTEGER NOT NULL,
  stars          INTEGER NOT NULL CHECK(stars BETWEEN 0 AND 3),
  moves_used     INTEGER NOT NULL,
  moves_total    INTEGER NOT NULL,
  duration_ms    INTEGER NOT NULL,
  booster_uses   INTEGER NOT NULL DEFAULT 0,
  creature_used  INTEGER,
  submitted_at   TEXT NOT NULL,
  client_ip      TEXT
);
CREATE INDEX IF NOT EXISTS idx_level_events_game_level  ON level_events(game_id, level_index, outcome);
CREATE INDEX IF NOT EXISTS idx_level_events_player_game ON level_events(player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_level_events_time        ON level_events(submitted_at DESC);

-- Client-side JS errors captured by window.onerror /
-- unhandledrejection handlers. Scoped per-game so dashboards can
-- attribute crashes to the right title.
CREATE TABLE IF NOT EXISTS error_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id       TEXT NOT NULL DEFAULT 'misfit-monsters',
  player_id     TEXT NOT NULL,
  message       TEXT NOT NULL,
  filename      TEXT,
  lineno        INTEGER,
  colno         INTEGER,
  stack         TEXT,
  url           TEXT,
  user_agent    TEXT,
  submitted_at  TEXT NOT NULL,
  client_ip     TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_events_time      ON error_events(submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_error_events_game_time ON error_events(game_id, submitted_at DESC);
