# Misfit Mountain Site

The Misfit Mountain Games web property — portfolio landing page plus the shared leaderboard / telemetry / profile API used by every Misfit game.

> **Where Misfits Belong.**

Live at **https://mountainstudio.cloud/**.

## What's in here

- `server/public/` — static portfolio site
  - `index.html` — landing page listing all Misfit games
  - `misfitmonsters/` — Misfit Monsters game page
  - `angrymonsters/` — Angry Monsters game page
- `server/app.py` — FastAPI backend (leaderboard, telemetry, profile)
- `server/schema.sql` + migrations — SQLite schema
- `server/nginx/`, `server/systemd/`, `server/deploy/` — VPS ops configs

## Games served by this property

- [Misfit Monsters](https://mountainstudio.cloud/misfitmonsters/) — match-3 puzzler (live)
- [Angry Monsters](https://mountainstudio.cloud/angrymonsters/) — physics puzzler (live)
- Misfit City — coming soon
- Misfit Bingo — planned

## Brand

| Color         | Hex       |
| ------------- | --------- |
| Midnight Deep | `#1A0F2E` |
| Mountain Purple | `#6B3FA0` |
| Ancient Gold  | `#D4A843` |
| Warm Cream    | `#FFF8F0` |

## Deploy

VPS auto-pulls `origin/main` every 60s via `mm-deploy.timer`. Push to main → live within ~60s. nginx serves the static site directly from `/opt/MisfitMountainSite/server/public`; the FastAPI process runs from `/opt/MisfitMountainSite/server` and proxies under `/api/*` on the same origin.

See [`server/README.md`](server/README.md) for full ops detail and first-time VPS setup.

## Run locally

Static site only:

```bash
cd server/public
python3 -m http.server 8000
# open http://localhost:8000/
```

API (requires Python 3.12+ and `pip install fastapi uvicorn`):

```bash
cd server
MM_DB_PATH=/tmp/scores.db sqlite3 /tmp/scores.db < schema.sql
MM_DB_PATH=/tmp/scores.db uvicorn app:app --host 127.0.0.1 --port 8001
```
