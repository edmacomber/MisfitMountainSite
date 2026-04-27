# Misfit Monsters API server

FastAPI + SQLite backend for the leaderboard + telemetry, deployed to
the Clean VPS (`mountainstudio.cloud`).

## Runtime layout on the VPS

```
/opt/MisfitMountainSite/       # git clone of this repo (the deploy target)
  └── server/                     # everything here

/var/www/mm-leaderboard/          # runtime / mutable data
  ├── venv/                       # python virtualenv (fastapi, uvicorn)
  └── scores.db                   # SQLite database (not in git)
```

The service reads `MM_DB_PATH` from the systemd unit to locate `scores.db`.
App code is served from `/opt/...` so `git pull` atomically updates it.

## Auto-deploy

`deploy/mm-deploy.timer` polls git every 60 seconds. When `server/` has
changed, `deploy/mm-deploy.sh` runs `git fetch && git reset --hard
origin/main` and restarts `mm-leaderboard.service`. No webhook secrets
to manage.

## First-time setup (manual)

See `docs/vps-setup.md` in this repo (written during the original
provisioning session). Abbreviated:

```bash
# System packages
sudo apt-get install -y nginx python3.12-venv certbot python3-certbot-nginx sqlite3

# Data dir + venv
sudo mkdir -p /var/www/mm-leaderboard
sudo chown claudecode:claudecode /var/www/mm-leaderboard
sudo -u claudecode python3 -m venv /var/www/mm-leaderboard/venv
sudo -u claudecode /var/www/mm-leaderboard/venv/bin/pip install fastapi uvicorn

# Clone the repo + init DB
sudo mkdir -p /opt
sudo chown claudecode:claudecode /opt
sudo -u claudecode git clone https://github.com/edmacomber/MisfitMountainSite.git /opt/MisfitMountainSite
sudo -u claudecode sqlite3 /var/www/mm-leaderboard/scores.db < /opt/MisfitMountainSite/server/schema.sql

# Install systemd units + nginx config
sudo cp /opt/MisfitMountainSite/server/systemd/mm-leaderboard.service /etc/systemd/system/
sudo cp /opt/MisfitMountainSite/server/deploy/mm-deploy.service /etc/systemd/system/
sudo cp /opt/MisfitMountainSite/server/deploy/mm-deploy.timer /etc/systemd/system/
sudo cp /opt/MisfitMountainSite/server/nginx/mm-leaderboard.conf /etc/nginx/sites-available/
sudo ln -sf /etc/nginx/sites-available/mm-leaderboard.conf /etc/nginx/sites-enabled/

# Sudoers entry so the deploy script can restart the service passwordlessly
echo 'claudecode ALL=(root) NOPASSWD: /bin/systemctl restart mm-leaderboard' \
  | sudo tee /etc/sudoers.d/mm-deploy

# Start everything
sudo systemctl daemon-reload
sudo systemctl enable --now mm-leaderboard
sudo systemctl enable --now mm-deploy.timer
sudo nginx -t && sudo systemctl reload nginx

# TLS
sudo certbot --nginx -d mountainstudio.cloud -d www.mountainstudio.cloud
```

## API surface

### Leaderboard
- `POST /api/leaderboard/submit` — record / improve a score.
- `GET /api/leaderboard/level/{n}` — top N scores for level.
- `GET /api/leaderboard/player/{id}` — all scores for a player.
- `GET /api/leaderboard/top` — overall rank by total stars + score.

### Telemetry
- `POST /api/events/level` — level-end event (complete / fail / quit).
- `POST /api/events/error` — client JS error.
- `GET /api/events/level/summary` — aggregate counts per level (dev).
- `GET /api/events/error/recent` — latest N errors (dev).
