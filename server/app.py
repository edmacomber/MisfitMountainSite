"""Mountain Studio leaderboard + telemetry API — multi-game version.

Same FastAPI app that powers Misfit Monsters, with a `game_id` column
added so a single SQLite database can hold leaderboards for multiple
games in the Misfit Mountain portfolio. Defaults preserve back-compat:
any request that omits `gameId` is treated as 'misfit-monsters'.

This file is a drop-in replacement for the existing
MisfitMonsters-Phaser/server/app.py once mc_migration.sql has been
applied to the database. Existing Misfit Monsters clients keep working
unchanged.

Endpoints:

  Leaderboard
    POST /submit                    — record / improve a score
    GET  /level/{n}?gameId=...      — top N for a (game, level) board
    GET  /player/{id}?gameId=...    — all scores for one player in a game
    GET  /top?gameId=...            — overall rank by total stars + score

  Telemetry (/events/*)
    POST /events/level              — record a level end (complete/fail/quit)
    POST /events/error              — record a JS error from a client

  Health
    GET  /health
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from threading import Lock
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("MM_DB_PATH", "/var/www/mm-leaderboard/scores.db")
MAX_LEVEL_INDEX = 99              # headroom: MM uses 0..49, MC uses 0..15
NAME_MAX_LEN = 16
LOCATION_MAX_LEN = 32             # "Maine, USA" / "Stockholm, Sweden" fit easily
PLAYER_ID_MIN = 8
PLAYER_ID_MAX = 64
GAME_ID_MAX_LEN = 32

DEFAULT_GAME_ID = "misfit-monsters"

# Nominatim (OpenStreetMap) public geocoder. Free, no API key, but
# requires a descriptive User-Agent and max 1 req/sec. That's fine for
# our scale: we only hit it on a genuine cache miss, which is once per
# unique location string across the lifetime of the leaderboard.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "misfit-monsters-leaderboard/1.0 (contact: edmacomber@gmail.com)"
NOMINATIM_TIMEOUT = 5.0

# Serialize Nominatim calls so we never exceed their 1 req/sec policy
# when multiple simultaneous /submits land on different locations.
_NOMINATIM_LOCK = Lock()
_NOMINATIM_LAST_CALL = 0.0
_NOMINATIM_MIN_INTERVAL = 1.1  # seconds

logger = logging.getLogger("mm")
logger.setLevel(logging.INFO)

app = FastAPI(title="Mountain Studio API", docs_url="/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://edmacomber.github.io",
        "http://localhost:8000",
        "http://localhost:3000",
        "http://localhost:3002",
        "http://localhost:3003",   # MisfitCity-Phaser dev
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=600,
)


# ── DB ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()


# ── Leaderboard schemas ────────────────────────────────────────────────
class SubmitRequest(BaseModel):
    gameId: str = Field(default=DEFAULT_GAME_ID, min_length=1, max_length=GAME_ID_MAX_LEN)
    playerId: str = Field(min_length=PLAYER_ID_MIN, max_length=PLAYER_ID_MAX)
    playerName: str = Field(min_length=1, max_length=NAME_MAX_LEN)
    # Optional — high-level "Region, Country" string shown alongside the
    # player name on the leaderboard. Clients that predate this field
    # just omit it; old rows keep NULL. Sanitized server-side.
    playerLocation: Optional[str] = Field(default=None, max_length=LOCATION_MAX_LEN)
    levelIndex: int = Field(ge=0, le=MAX_LEVEL_INDEX)
    score: int = Field(ge=0, le=9999999)
    stars: int = Field(ge=0, le=3)


class ScoreEntry(BaseModel):
    playerName: str
    playerLocation: Optional[str] = None
    playerLat: Optional[float] = None
    playerLng: Optional[float] = None
    score: int
    stars: int
    submittedAt: str


class SubmitResponse(BaseModel):
    rank: int
    totalEntries: int
    bestForPlayer: int
    isNewBest: bool


class ProfileLocationRequest(BaseModel):
    gameId: str = Field(default=DEFAULT_GAME_ID, min_length=1, max_length=GAME_ID_MAX_LEN)
    playerId: str = Field(min_length=PLAYER_ID_MIN, max_length=PLAYER_ID_MAX)
    playerName: str = Field(min_length=1, max_length=NAME_MAX_LEN)
    playerLocation: Optional[str] = Field(default=None, max_length=LOCATION_MAX_LEN)


class ProfileLocationResponse(BaseModel):
    ok: bool
    location: Optional[str] = None
    rowsUpdated: int = 0
    geocodeQueued: bool = False


class LevelResponse(BaseModel):
    levelIndex: int
    entries: List[ScoreEntry]
    myRank: Optional[int] = None
    myScore: Optional[int] = None


# ── Telemetry schemas ──────────────────────────────────────────────────
class LevelEventRequest(BaseModel):
    gameId: str = Field(default=DEFAULT_GAME_ID, min_length=1, max_length=GAME_ID_MAX_LEN)
    playerId: str = Field(min_length=PLAYER_ID_MIN, max_length=PLAYER_ID_MAX)
    levelIndex: int = Field(ge=0, le=MAX_LEVEL_INDEX)
    outcome: str = Field(pattern="^(complete|fail|quit)$")
    score: int = Field(ge=0, le=9999999)
    stars: int = Field(ge=0, le=3)
    movesUsed: int = Field(ge=0, le=200)
    movesTotal: int = Field(ge=1, le=200)
    durationMs: int = Field(ge=0, le=3600000)
    boosterUses: int = Field(ge=0, le=50)
    creatureUsed: Optional[int] = Field(default=None, ge=0, le=20)


class ErrorEventRequest(BaseModel):
    gameId: str = Field(default=DEFAULT_GAME_ID, min_length=1, max_length=GAME_ID_MAX_LEN)
    playerId: str = Field(min_length=PLAYER_ID_MIN, max_length=PLAYER_ID_MAX)
    message: str = Field(min_length=1, max_length=2000)
    filename: Optional[str] = Field(default=None, max_length=500)
    lineno: Optional[int] = Field(default=None, ge=0, le=1000000)
    colno: Optional[int] = Field(default=None, ge=0, le=1000000)
    stack: Optional[str] = Field(default=None, max_length=8000)
    url: Optional[str] = Field(default=None, max_length=1000)
    userAgent: Optional[str] = Field(default=None, max_length=500)


# ── Helpers ─────────────────────────────────────────────────────────────
_NAME_RE = re.compile(r"[^\w\s'\-.]", flags=re.UNICODE)
# Location allows commas in addition to the name-safe set — "Maine, USA"
# is the canonical shape we're asking players to use, so the comma has
# to survive sanitization.
_LOCATION_RE = re.compile(r"[^\w\s'\-.,]", flags=re.UNICODE)

# First-pass profanity filter for display names + locations. Small
# hand-curated list of obvious offenders — catches casual abuse without
# pulling in an external dictionary dependency. Matched against
# lowercased, character-substituted ("l33t speak" neutralized) token
# boundaries so "f_ck" or "f.u.c.k" trip the same as "fuck" but
# "scunthorpe" / "Cocke" / "assumption" don't (substring match would
# false-positive heavily).
#
# When a term trips: the server rejects the submission with HTTP 400,
# the client keeps whatever the previously-saved clean value was.
# Players who really want to submit offensive content can always
# bypass a server-side list — treat this as a speed bump, not a wall.
# The geocoder-clearing path (for locations) + future
# community-reporting + (eventual) LLM moderation layer form the real
# defense in depth.
_PROFANITY_TERMS = {
    # Slurs (ethnic, sexual-orientation, misogynistic) \u2014 the hard red
    # lines. Kept intentionally short so the filter stays readable.
    "nigger", "nigga", "faggot", "fag", "tranny", "retard",
    "kike", "spic", "chink", "gook", "cunt", "whore", "slut",
    # Top-tier obscenities
    "fuck", "fucker", "motherfucker", "shit", "bitch", "asshole",
    "bastard", "dick", "cock", "pussy",
    # Naughty-body obvious
    "penis", "vagina",
    # Sexual aggression / CSAM adjacency \u2014 never wanted, easy catch
    "rape", "rapist", "pedo", "pedophile", "nazi", "hitler",
}

_L33T_TRANS = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i",
})


def _contains_profanity(s: str) -> bool:
    """True if any \u2605 *token* \u2605 in `s` (after lowercase + l33t-speak
    canonicalization + collapsing non-letters) matches a curated bad
    word. Token-level match means 'class' and 'assume' don't trip the
    'ass' filter, which is the whole point.
    """
    if not s:
        return False
    canonical = s.lower().translate(_L33T_TRANS)
    # Split on ANY non-letter boundary so "f.u.c.k" \u2192 "fuck" after the
    # join-per-token step below.
    raw_tokens = re.split(r"[^a-z]+", canonical)
    # Also a single concatenated form: "fuckyou" as one token still matches.
    joined = "".join(raw_tokens)
    for tok in raw_tokens:
        if tok in _PROFANITY_TERMS:
            return True
    for term in _PROFANITY_TERMS:
        if term in joined:
            return True
    return False


def sanitize_name(name: str) -> str:
    cleaned = _NAME_RE.sub("", name).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise HTTPException(status_code=400, detail="display name is empty")
    if _contains_profanity(cleaned):
        # Refuse to store. Client-side the submission returns http-400,
        # leaderboard.js resolves with ok:false and gameplay carries on;
        # any score saved offline syncs with the next clean name the
        # player sets. Intentionally vague error so probing the list is
        # harder.
        raise HTTPException(status_code=400, detail="display name not accepted")
    return cleaned[:NAME_MAX_LEN]


def sanitize_location(loc: Optional[str]) -> Optional[str]:
    """Location sanitizer — same strip + collapse as the name, but:
      - An empty/missing location is valid and returns None (the
        default state for players who haven't opted in).
      - 32-char cap so "Stockholm, Sweden"-size strings fit.
      - Uses _LOCATION_RE so commas survive (canonical format is
        "Region, Country").
      - Rejects entries that fail the profanity check upfront
        (belt-and-suspenders with the "Nominatim said it isn't a
        real place" clearing in geocode_and_apply).
    Accepts None directly so handlers don't need a pre-check.
    """
    if loc is None:
        return None
    cleaned = _LOCATION_RE.sub("", loc).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    if _contains_profanity(cleaned):
        raise HTTPException(status_code=400, detail="location not accepted")
    return cleaned[:LOCATION_MAX_LEN]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def client_ip_of(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ── Geocoding ───────────────────────────────────────────────────────────
def _location_cache_key(loc: str) -> str:
    """Normalize a location string into the cache lookup key. Lowercase,
    collapse whitespace, strip. 'Maine, USA' and 'maine, usa' map to the
    same cache row — one Nominatim call covers both.
    """
    return " ".join(loc.lower().split())


def _cache_lookup(db: sqlite3.Connection, key: str):
    row = db.execute(
        "SELECT lat, lng FROM geocode_cache WHERE location_key = ?",
        (key,),
    ).fetchone()
    if row:
        return row["lat"], row["lng"]
    return None


def _cache_store(db: sqlite3.Connection, key: str, lat: float, lng: float, display_name: str):
    db.execute(
        "INSERT OR REPLACE INTO geocode_cache"
        " (location_key, lat, lng, display_name, resolved_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (key, lat, lng, display_name, now_iso()),
    )


# Sentinel distinguishing "Nominatim said this is not a real place" from
# "we couldn't reach Nominatim / transient error". The former is a
# reliable signal that the location string is garbage (or offensive) and
# should be cleared from the player's profile. The latter should never
# punish a legit user — we just leave the string intact and try again
# next time.
GEOCODE_UNRESOLVED = object()  # returned on confirmed "no such place"


def _nominatim_geocode(query: str):
    """Hit Nominatim once, respecting the 1 req/sec policy via a
    serializing lock.

    Returns one of:
      * (lat, lng, display_name) on success
      * GEOCODE_UNRESOLVED when Nominatim responded cleanly with an
        empty result set \u2014 strong signal the string isn't a real
        place and callers should treat it as bad input
      * None on transient/network failure \u2014 leave the location
        untouched and try again on the next geocode pass
    """
    global _NOMINATIM_LAST_CALL
    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0,
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": NOMINATIM_UA})
    with _NOMINATIM_LOCK:
        # Honour the 1 req/sec rate limit.
        delta = time.time() - _NOMINATIM_LAST_CALL
        if delta < _NOMINATIM_MIN_INTERVAL:
            time.sleep(_NOMINATIM_MIN_INTERVAL - delta)
        _NOMINATIM_LAST_CALL = time.time()
        try:
            with urllib.request.urlopen(req, timeout=NOMINATIM_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("nominatim lookup failed for %r: %s", query, exc)
            return None
    if not data:
        # Clean response, no candidates \u2014 the string isn't a place.
        return GEOCODE_UNRESOLVED
    first = data[0]
    try:
        lat = float(first["lat"])
        lng = float(first["lon"])
    except (KeyError, ValueError, TypeError):
        return GEOCODE_UNRESOLVED
    return lat, lng, first.get("display_name", "")


def geocode_and_apply(game_id: str, player_id: str, location: str):
    """FastAPI BackgroundTask: look up `location`, persist lat/lng on
    every row for (game_id, player_id). Runs after /submit responds so
    the user's level-complete UI never waits on a Nominatim round trip.

    Three outcomes:
      * Success \u2192 lat/lng written to every row for this player.
      * Confirmed unresolved (Nominatim said the string isn't a place)
        \u2192 clear player_location + player_lat + player_lng on every
        row. Acts as a soft moderation filter: vulgar /
        non-geographic / nonsense strings never land on the
        leaderboard or globe.
      * Transient failure (network timeout, etc.) \u2192 leave the
        location string alone and try again next pass.
    """
    key = _location_cache_key(location)
    if not key:
        return
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        cached = _cache_lookup(conn, key)
        if cached:
            lat, lng = cached
        else:
            result = _nominatim_geocode(location)
            if result is None:
                # Transient failure \u2014 silently give up this pass.
                return
            if result is GEOCODE_UNRESOLVED:
                # Confirmed not-a-place: clear the offending string
                # from every row this player has. They can try a
                # different string later.
                logger.info("clearing unresolvable location %r for player %s",
                            location, player_id)
                conn.execute(
                    "UPDATE scores SET player_location = NULL,"
                    " player_lat = NULL, player_lng = NULL"
                    " WHERE game_id = ? AND player_id = ?",
                    (game_id, player_id),
                )
                return
            lat, lng, display = result
            _cache_store(conn, key, lat, lng, display)
        conn.execute(
            "UPDATE scores SET player_lat = ?, player_lng = ?"
            " WHERE game_id = ? AND player_id = ?",
            (lat, lng, game_id, player_id),
        )
    except Exception as exc:
        logger.warning("geocode_and_apply failed for %r / %r: %s",
                       player_id, location, exc)
    finally:
        conn.close()


# ── Health ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "1.4.2-moderation"}


# ── Leaderboard ─────────────────────────────────────────────────────────
@app.post("/submit", response_model=SubmitResponse)
def submit_score(
    req: SubmitRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
):
    name = sanitize_name(req.playerName)
    # Location is opt-in — absent/empty stays NULL on the row. This keeps
    # pre-1.3.0 behaviour for clients that don't send the field.
    location = sanitize_location(req.playerLocation)
    now = now_iso()
    client_ip = client_ip_of(request)

    row = db.execute(
        "SELECT id, score FROM scores"
        " WHERE game_id = ? AND player_id = ? AND level_index = ?",
        (req.gameId, req.playerId, req.levelIndex),
    ).fetchone()

    is_new_best = False
    if row is None:
        db.execute(
            "INSERT INTO scores"
            " (game_id, player_id, player_name, player_location, level_index,"
            "  score, stars, submitted_at, client_ip)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req.gameId, req.playerId, name, location, req.levelIndex,
             req.score, req.stars, now, client_ip),
        )
        best = req.score
        is_new_best = True
    elif req.score > row["score"]:
        db.execute(
            "UPDATE scores SET player_name = ?, player_location = ?,"
            " score = ?, stars = ?, submitted_at = ?, client_ip = ?"
            " WHERE id = ?",
            (name, location, req.score, req.stars, now, client_ip, row["id"]),
        )
        best = req.score
        is_new_best = True
    else:
        db.execute(
            "UPDATE scores SET player_name = ?, player_location = ? WHERE id = ?",
            (name, location, row["id"]),
        )
        best = row["score"]

    # Mirror the player's name + location across every row they have in
    # this game so the /top aggregation (which uses MAX(player_location))
    # sees a single consistent value per player. Cheap — one player
    # holds at most ~45 rows (one per MM level). Without this, changing
    # the display name or location would only update the level just
    # submitted and the leaderboard would show mixed stale values.
    if location is not None:
        db.execute(
            "UPDATE scores SET player_name = ?, player_location = ?"
            " WHERE game_id = ? AND player_id = ?",
            (name, location, req.gameId, req.playerId),
        )
    else:
        # Only fan out name; leave existing locations untouched if the
        # current submit didn't include one (a location-less client
        # shouldn't wipe a location set earlier by a newer client).
        db.execute(
            "UPDATE scores SET player_name = ?"
            " WHERE game_id = ? AND player_id = ?",
            (name, req.gameId, req.playerId),
        )

    rank = db.execute(
        "SELECT COUNT(*) + 1 AS rank FROM scores"
        " WHERE game_id = ? AND level_index = ? AND score > ?",
        (req.gameId, req.levelIndex, best),
    ).fetchone()["rank"]
    total = db.execute(
        "SELECT COUNT(*) AS total FROM scores"
        " WHERE game_id = ? AND level_index = ?",
        (req.gameId, req.levelIndex),
    ).fetchone()["total"]

    # Geocode in the background so /submit returns instantly. Only
    # schedule if we actually have a location string; otherwise
    # there's nothing to geocode. The background task opens its own
    # DB connection and updates every row for (game, player) with
    # the resolved lat/lng once Nominatim responds (or hits cache).
    if location:
        background_tasks.add_task(
            geocode_and_apply, req.gameId, req.playerId, location,
        )

    return SubmitResponse(
        rank=rank, totalEntries=total, bestForPlayer=best, isNewBest=is_new_best,
    )


@app.post("/profile/location", response_model=ProfileLocationResponse)
def update_profile_location(
    req: ProfileLocationRequest,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
):
    """Update the player's display name + location across every row
    they have for this game, and kick off background geocoding so the
    globe picks them up without needing a score submission. Called
    from Settings when the player saves a location — means their pin
    appears on the globe within a couple seconds of saving, rather
    than waiting on their next level win.

    Empty/missing location clears the location+lat/lng. No rows yet?
    Returns rowsUpdated=0 — the player's name/location are still
    accepted by a future /submit because sanitization is by-field.
    """
    name = sanitize_name(req.playerName)
    location = sanitize_location(req.playerLocation)
    if location is None:
        # Clearing — null out location + lat/lng for every row, no
        # geocoding needed.
        cur = db.execute(
            "UPDATE scores SET player_name = ?, player_location = NULL,"
            " player_lat = NULL, player_lng = NULL"
            " WHERE game_id = ? AND player_id = ?",
            (name, req.gameId, req.playerId),
        )
        return ProfileLocationResponse(
            ok=True, location=None, rowsUpdated=cur.rowcount or 0,
            geocodeQueued=False,
        )
    # Write the location (and name) immediately. lat/lng will follow
    # when the background task resolves.
    cur = db.execute(
        "UPDATE scores SET player_name = ?, player_location = ?"
        " WHERE game_id = ? AND player_id = ?",
        (name, location, req.gameId, req.playerId),
    )
    background_tasks.add_task(
        geocode_and_apply, req.gameId, req.playerId, location,
    )
    return ProfileLocationResponse(
        ok=True, location=location, rowsUpdated=cur.rowcount or 0,
        geocodeQueued=True,
    )


@app.get("/level/{level_index}", response_model=LevelResponse)
def get_level(
    level_index: int,
    limit: int = 20,
    gameId: str = DEFAULT_GAME_ID,
    playerId: Optional[str] = None,
    db=Depends(get_db),
):
    if level_index < 0 or level_index > MAX_LEVEL_INDEX:
        raise HTTPException(status_code=400, detail="invalid level_index")
    limit = max(1, min(limit, 100))

    rows = db.execute(
        "SELECT player_name, player_location, player_lat, player_lng,"
        " score, stars, submitted_at FROM scores"
        " WHERE game_id = ? AND level_index = ?"
        " ORDER BY score DESC, submitted_at ASC LIMIT ?",
        (gameId, level_index, limit),
    ).fetchall()

    entries = [
        ScoreEntry(
            playerName=r["player_name"],
            playerLocation=r["player_location"],
            playerLat=r["player_lat"],
            playerLng=r["player_lng"],
            score=r["score"],
            stars=r["stars"],
            submittedAt=r["submitted_at"],
        )
        for r in rows
    ]

    my_rank = my_score = None
    if playerId:
        me = db.execute(
            "SELECT score FROM scores"
            " WHERE game_id = ? AND player_id = ? AND level_index = ?",
            (gameId, playerId, level_index),
        ).fetchone()
        if me:
            my_score = me["score"]
            my_rank = db.execute(
                "SELECT COUNT(*) + 1 AS rank FROM scores"
                " WHERE game_id = ? AND level_index = ? AND score > ?",
                (gameId, level_index, my_score),
            ).fetchone()["rank"]

    return LevelResponse(
        levelIndex=level_index,
        entries=entries,
        myRank=my_rank,
        myScore=my_score,
    )


@app.get("/player/{player_id}")
def get_player(player_id: str, gameId: str = DEFAULT_GAME_ID, db=Depends(get_db)):
    rows = db.execute(
        "SELECT level_index, player_name, score, stars, submitted_at FROM scores"
        " WHERE game_id = ? AND player_id = ? ORDER BY level_index",
        (gameId, player_id),
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.get("/top")
def get_top(limit: int = 20, gameId: str = DEFAULT_GAME_ID, db=Depends(get_db)):
    limit = max(1, min(limit, 100))
    # MAX(player_location) / MAX(player_lat) / MAX(player_lng) are safe
    # because /submit fans the latest location out to every row for
    # (game_id, player_id) and the geocode background task does the
    # same with lat/lng — every row for one player holds the same
    # value, so the aggregates trivially collapse. For pre-migration
    # rows the columns are still NULL and that NULL propagates here.
    rows = db.execute(
        """
        SELECT
          player_id,
          player_name,
          MAX(player_location) AS player_location,
          MAX(player_lat)      AS player_lat,
          MAX(player_lng)      AS player_lng,
          SUM(stars)           AS total_stars,
          SUM(score)           AS total_score,
          COUNT(*)             AS levels_played,
          MAX(submitted_at)    AS last_active
        FROM scores
        WHERE game_id = ?
        GROUP BY player_id
        ORDER BY total_stars DESC, total_score DESC
        LIMIT ?
        """,
        (gameId, limit),
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


# ── Telemetry ───────────────────────────────────────────────────────────
@app.post("/events/level")
def submit_level_event(
    req: LevelEventRequest, request: Request, db=Depends(get_db),
):
    db.execute(
        "INSERT INTO level_events"
        " (game_id, player_id, level_index, outcome, score, stars,"
        "  moves_used, moves_total, duration_ms, booster_uses,"
        "  creature_used, submitted_at, client_ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            req.gameId, req.playerId, req.levelIndex, req.outcome, req.score, req.stars,
            req.movesUsed, req.movesTotal, req.durationMs, req.boosterUses,
            req.creatureUsed, now_iso(), client_ip_of(request),
        ),
    )
    return {"recorded": True}


@app.post("/events/error")
def submit_error_event(
    req: ErrorEventRequest, request: Request, db=Depends(get_db),
):
    db.execute(
        "INSERT INTO error_events"
        " (game_id, player_id, message, filename, lineno, colno, stack,"
        "  url, user_agent, submitted_at, client_ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            req.gameId, req.playerId, req.message, req.filename, req.lineno,
            req.colno, req.stack, req.url, req.userAgent,
            now_iso(), client_ip_of(request),
        ),
    )
    return {"recorded": True}


# ── Analytics-helper read endpoints ────────────────────────────────────
@app.get("/events/level/summary")
def level_summary(
    limit: int = 50, gameId: str = DEFAULT_GAME_ID, db=Depends(get_db),
):
    limit = max(1, min(limit, 100))
    rows = db.execute(
        """
        SELECT
          level_index,
          outcome,
          COUNT(*) AS n,
          AVG(duration_ms) AS avg_ms,
          AVG(score)       AS avg_score,
          AVG(stars)       AS avg_stars
        FROM level_events
        WHERE game_id = ?
        GROUP BY level_index, outcome
        ORDER BY level_index, outcome
        LIMIT ?
        """,
        (gameId, limit * 3),
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.get("/events/error/recent")
def recent_errors(
    limit: int = 20,
    gameId: Optional[str] = None,
    db=Depends(get_db),
):
    """Recent errors. Pass ?gameId=… to scope to one game; omit to get a
    cross-game stream. game_id is included in every row so callers can
    demultiplex without a second query."""
    limit = max(1, min(limit, 100))
    if gameId:
        rows = db.execute(
            "SELECT id, game_id, player_id, message, filename, lineno,"
            " url, submitted_at FROM error_events"
            " WHERE game_id = ? ORDER BY id DESC LIMIT ?",
            (gameId, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, game_id, player_id, message, filename, lineno,"
            " url, submitted_at FROM error_events"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"entries": [dict(r) for r in rows]}
