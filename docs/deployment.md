# Deployment: single AWS EC2 t3.small (Ubuntu)

Stack: app (FastAPI), Postgres, Redis and Caddy (automatic HTTPS), run by Docker Compose.
Replace `YOUR_DOMAIN` (for example `calls.example.com`) and `YOUR_REPO_URL` below.

## 1. AWS: launch and open ports

1. Launch an Ubuntu 24.04 (or 22.04) `t3.small` instance with a 20 GB+ gp3 disk.
2. Allocate an **Elastic IP** and attach it, so the address never changes.
3. Security group, inbound rules:

| Port | Protocol | Source | Why |
|---|---|---|---|
| 22 | TCP | **My IP** only | SSH |
| 80 | TCP | 0.0.0.0/0 | Certificate issuance and HTTP→HTTPS redirect |
| 443 | TCP | 0.0.0.0/0 | HTTPS and WebSocket (wss) |
| 443 | UDP | 0.0.0.0/0 | HTTP/3 (optional) |

Do not open 5432 (Postgres), 6379 (Redis) or 8000 (app). They are not published by Compose.

## 2. DNS

At your DNS provider, create an **A record**: `YOUR_DOMAIN` → the Elastic IP.
Check it before the first start (Caddy cannot get a certificate until this resolves):

```bash
dig +short YOUR_DOMAIN     # must print the Elastic IP
```

## 3. SSH in and run the setup script

```bash
ssh -i /path/to/key.pem ubuntu@YOUR_DOMAIN
sudo apt-get update && sudo apt-get install -y git
git clone YOUR_REPO_URL ~/kodera
cd ~/kodera
bash scripts/server_setup.sh     # swap + Docker. Safe to run again.
exit                             # log out so the docker group applies
```

(For a private repo, use a GitHub deploy key or a personal access token for the clone.)

SSH back in and check:

```bash
ssh -i /path/to/key.pem ubuntu@YOUR_DOMAIN
docker ps            # works without sudo
free -h              # shows 2 GB swap
```

## 4. Create `.env`

```bash
cd ~/kodera
cp .env.example .env
chmod 600 .env
nano .env
```

Set at least:

- `DOMAIN=YOUR_DOMAIN`
- `PUBLIC_BASE_URL=https://YOUR_DOMAIN`
- `POSTGRES_PASSWORD`: generate with `openssl rand -hex 24`
- `DATABASE_URL`: same password: `postgresql+asyncpg://app:THE_PASSWORD@postgres:5432/app`
- Telnyx keys, `LLM_*`, `STT_BASE_URL`, `TTS_BASE_URL` (GPU server addresses), `GATEWAY_API_KEY`

Never commit `.env`. `deploy.sh` refuses to run while `change-me` or `replace-me` remain in the password lines.

> Postgres only reads `POSTGRES_PASSWORD` the **first** time it creates its volume. Changing it
> later in `.env` does not change the existing database password.

## 5. First start

```bash
cd ~/kodera
bash scripts/deploy.sh
```

It builds the images, starts everything, waits for health checks and prints the status.
If a service is unhealthy it prints its last logs. The first build takes a few minutes.

Check:

```bash
curl -i https://YOUR_DOMAIN/health        # expect HTTP/2 200
```

The first HTTPS request may fail for up to a minute while Caddy obtains the certificate.
If it keeps failing: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs caddy`
(usually DNS not pointing at the server yet, or port 80/443 closed).

Point Telnyx at: webhook `https://YOUR_DOMAIN/webhooks/telnyx` (confirm the path in
`app/api/routes/webhooks.py`) and media stream `wss://YOUR_DOMAIN/ws/...`.

Tip: save a shortcut so the commands below are shorter:

```bash
echo "alias dc='docker compose -f ~/kodera/docker-compose.yml -f ~/kodera/docker-compose.prod.yml'" >> ~/.bashrc
source ~/.bashrc
```

## 6. Update

```bash
cd ~/kodera && bash scripts/deploy.sh
```

This runs `git pull --ff-only`, rebuilds and restarts only what changed.

## 7. Logs and status

```bash
dc ps                       # status and health
dc logs -f --tail 100 app   # follow app logs (Ctrl+C to stop)
dc logs --tail 100 caddy
docker stats --no-stream    # memory use per container (limits: app 512M, postgres 384M, redis 192M, caddy 64M)
```

Logs rotate automatically (10 MB × 3 files per service).

## 8. Back up Postgres

```bash
mkdir -p ~/backups
dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > ~/backups/kodera-$(date +%F-%H%M).sql.gz
ls -lh ~/backups
```

Copy backups off the server regularly (for example `scp` to your laptop, or S3), because the
disk is the only copy otherwise. Restore into a running stack:

```bash
gunzip -c ~/backups/FILE.sql.gz | dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

(To restore over existing data, restore into an empty database; see the `pg_dump`/`psql` docs.)

## 9. Roll back

If a deploy broke something, go back to the last good commit:

```bash
cd ~/kodera
git log --oneline -10                      # find the good commit
git checkout GOOD_COMMIT_SHA
SKIP_PULL=1 bash scripts/deploy.sh         # redeploy this checkout without pulling
```

To return to the latest code later: `git checkout main && bash scripts/deploy.sh`.
Database migrations are not rolled back automatically. Restore a backup if one was applied.

## 10. Useful operations

```bash
dc restart app                  # restart one service
dc down                         # stop everything (data volumes are kept)
dc down -v                      # DANGER: also deletes Postgres/Redis/Caddy data
docker system prune -f          # reclaim disk from old images
```

## Later option

A GitHub Actions workflow could SSH into the server and run `bash scripts/deploy.sh` on every
push to `main`. Not set up yet.
