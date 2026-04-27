# Project conventions — MisfitMountainSite

## What this repo is

The shared web property for Misfit Mountain Games: the portfolio site at https://mountainstudio.cloud/ plus the FastAPI leaderboard / telemetry / profile API serving every Misfit game. Was extracted from `MisfitMonsters-Phaser` so individual games no longer carry shared infra.

## Static site

- All HTML lives under `server/public/`. No build step. All CSS is inline in each page.
- Pages use absolute paths (`/misfitmonsters/`, `/angrymonsters/`) — no `../` traversal.
- Game art assets are hosted out-of-repo on GitHub Pages at `https://edmacomber.github.io/MisfitMonsters-Phaser/` and referenced by absolute URL.
- Brand color tokens (use these consistently in any new page):
  - Midnight Deep `#1A0F2E`
  - Mountain Purple `#6B3FA0`
  - Ancient Gold `#D4A843`
  - Warm Cream `#FFF8F0`

## API

- FastAPI + SQLite. Single process, runs on `127.0.0.1:8001` behind nginx.
- DB path comes from env: `MM_DB_PATH=/var/www/mm-leaderboard/scores.db` on the VPS.
- Runtime data (`scores.db`, venv) lives at `/var/www/mm-leaderboard/` — never in this repo.
- Endpoints: `/api/leaderboard/*`, `/api/events/*`, `/api/profile/*`.

## Deploy

- VPS-only. `mm-deploy.timer` polls `origin/main` every 60s and `git reset --hard`. ~60s from `git push` to live.
- Server file changes restart `mm-leaderboard.service` automatically. Static-only changes do too (false positive — harmless).
- **Do not** move the static site to GitHub Pages. The site and API must stay same-origin so game clients can call `/api/*` without CORS.

## Working in this repo

- Default to no comments; add only when intent is non-obvious.
- Do not create planning/decision docs in the repo.
- When adding a new game's landing page: create `server/public/<gamename>/index.html`, link from the root `index.html` game grid, follow the brand color tokens.
