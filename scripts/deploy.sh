#!/usr/bin/env bash
# Update and (re)start the stack on the server. Run from anywhere:
#   bash scripts/deploy.sh            # git pull, then build and start
#   SKIP_PULL=1 bash scripts/deploy.sh  # redeploy the current checkout (e.g. rollback)
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
TIMEOUT="${HEALTH_TIMEOUT:-180}"

[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env && nano .env" >&2; exit 1; }

# Refuse to start with leftover placeholders.
if grep -Eq '^(POSTGRES_PASSWORD|DATABASE_URL)=.*(change-me|replace-me)' .env; then
  echo ".env still contains placeholder values (change-me / replace-me). Fill them in." >&2
  exit 1
fi
if ! grep -Eq '^DOMAIN=.+' .env || grep -Eq '^DOMAIN=.*example\.com' .env; then
  echo "Set DOMAIN in .env to your real domain." >&2
  exit 1
fi

if [ -z "${SKIP_PULL:-}" ]; then
  echo "==> git pull"
  git pull --ff-only
fi
echo "==> Deploying commit $(git rev-parse --short HEAD)"

echo "==> Building and starting"
"${COMPOSE[@]}" up -d --build --remove-orphans

echo "==> Waiting up to ${TIMEOUT}s for services to become healthy"
deadline=$((SECONDS + TIMEOUT))
bad=""
while :; do
  ids=$("${COMPOSE[@]}" ps -q)
  bad=""
  for id in $ids; do
    state=$(docker inspect -f '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
    name=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id")
    case "$state" in
      running\|healthy|running\|none) ;;
      *) bad="$bad $name($state)" ;;
    esac
  done
  [ -z "$bad" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then break; fi
  sleep 3
done

echo
"${COMPOSE[@]}" ps

if [ -n "$bad" ]; then
  echo
  echo "NOT HEALTHY:$bad" >&2
  for svc in $(echo "$bad" | tr ' ' '\n' | sed 's/(.*//' | grep .); do
    echo "---- last 50 log lines: $svc ----" >&2
    "${COMPOSE[@]}" logs --tail 50 "$svc" >&2 || true
  done
  exit 1
fi

echo
echo "All services healthy. Check: curl -i https://$(grep -E '^DOMAIN=' .env | cut -d= -f2-)/health"
