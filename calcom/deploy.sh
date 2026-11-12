#!/usr/bin/env bash
# Manage Cal.diy on the server. Run from anywhere:
#   bash calcom/deploy.sh init      make calcom/.env with fresh secrets (once)
#   bash calcom/deploy.sh up        create its database, wire HTTPS, pull images, start
#   bash calcom/deploy.sh update    pull newer images and restart
#   bash calcom/deploy.sh status    containers, memory, health
#   bash calcom/deploy.sh logs [web|api]
#   bash calcom/deploy.sh backup    dump the Cal.diy database to calcom/backups/
#   bash calcom/deploy.sh down      stop Cal.diy (data is kept)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

MAIN=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
CAL=(docker compose --env-file calcom/.env -f calcom/docker-compose.yml)
ENV_FILE=calcom/.env

need_env() { [ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE. Run: bash calcom/deploy.sh init" >&2; exit 1; }; }
getv() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

cmd_init() {
  [ -f "$ENV_FILE" ] && { echo "$ENV_FILE already exists; edit it by hand."; return; }
  cp calcom/.env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  for name in CAL_DB_PASSWORD NEXTAUTH_SECRET CALENDSO_ENCRYPTION_KEY JWT_SECRET; do
    sed -i "s|^$name=.*|$name=$(openssl rand -hex 24)|" "$ENV_FILE"
  done
  echo "Created $ENV_FILE with new secrets. Now edit CAL_DOMAIN, CAL_PUBLIC_URL and the two images,"
  echo "then run: bash calcom/deploy.sh up"
}

check_ready() {
  need_env
  if grep -Eq '^(CAL_DOMAIN|CAL_PUBLIC_URL)=.*example\.com|YOUR_GITHUB_USER' "$ENV_FILE"; then
    echo "Fill in CAL_DOMAIN, CAL_PUBLIC_URL and the image names in $ENV_FILE first." >&2; exit 1
  fi
  local total_mb; total_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  if [ "$total_mb" -lt 3500 ]; then
    echo "WARNING: this server has ${total_mb} MB RAM. Cal.diy needs about 1.5-2 GB on top of the" >&2
    echo "Kodera stack; below ~4 GB it can starve the call agent. Read calcom/README.md (Server size)." >&2
    [ "${FORCE:-}" = "1" ] || { echo "Re-run with FORCE=1 to continue anyway." >&2; exit 1; }
  fi
  "${MAIN[@]}" ps -q postgres >/dev/null 2>&1 || { echo "The Kodera stack is not running." >&2; exit 1; }
}

create_database() {
  local pw; pw=$(getv CAL_DB_PASSWORD)
  echo "==> Database 'calcom' in the existing Postgres"
  "${MAIN[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-app}" -d postgres <<SQL
SELECT 'CREATE ROLE calcom LOGIN PASSWORD ''$pw''' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='calcom')\gexec
ALTER ROLE calcom PASSWORD '$pw';
SELECT 'CREATE DATABASE calcom OWNER calcom' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='calcom')\gexec
SQL
}

wire_https() {
  local domain; domain=$(getv CAL_DOMAIN)
  echo "==> HTTPS for $domain (needs a DNS A record pointing at this server)"
  mkdir -p docker/conf.d
  sed "s|__CAL_DOMAIN__|$domain|" calcom/caddy/calcom.caddy.tmpl > docker/conf.d/calcom.caddy
  # Check the config in a throwaway container before touching the running Caddy.
  docker run --rm -v "$ROOT/docker/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$ROOT/docker/conf.d:/etc/caddy/conf.d:ro" -e DOMAIN="$(grep -E '^DOMAIN=' .env | cut -d= -f2-)" \
    caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile >/dev/null
  "${MAIN[@]}" up -d caddy  # picks up the new conf.d mount if this is the first time
  "${MAIN[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile
}

cmd_up() {
  check_ready
  create_database
  echo "==> Pulling images"
  "${CAL[@]}" pull
  echo "==> Starting Cal.diy (the first start runs migrations and takes a few minutes)"
  "${CAL[@]}" up -d
  wire_https
  echo
  cmd_status
  echo
  echo "Open $(getv CAL_PUBLIC_URL) and finish setup: calcom/README.md, step 5."
}

cmd_update() { check_ready; "${CAL[@]}" pull; "${CAL[@]}" up -d; cmd_status; }

cmd_status() {
  need_env
  "${CAL[@]}" ps
  docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' | grep -E 'NAME|calcom|kodera' || true
  free -m | head -2
}

cmd_logs() {
  need_env
  case "${1:-}" in web) svc=calcom-web ;; api) svc=calcom-api ;; *) svc= ;; esac
  "${CAL[@]}" logs --tail 100 -f $svc
}

cmd_backup() {
  need_env
  mkdir -p calcom/backups
  local out="calcom/backups/calcom-$(date +%F-%H%M%S).sql.gz"
  "${MAIN[@]}" exec -T postgres pg_dump -U calcom -d calcom | gzip > "$out"
  ls -1t calcom/backups/*.sql.gz | tail -n +8 | xargs -r rm -f   # keep the newest 7
  echo "Saved $out ($(du -h "$out" | cut -f1))"
}

cmd_down() { need_env; "${CAL[@]}" down; }

case "${1:-}" in
  init) cmd_init ;;
  up) cmd_up ;;
  update) cmd_update ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "${1:-}" ;;
  backup) cmd_backup ;;
  down) cmd_down ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
