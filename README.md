# AI Calling Agent

A self-hosted AI voice agent for US phone calls. Python 3.12 + FastAPI, Telnyx media streams,
and a Pipecat voice pipeline. The STT, LLM and TTS models run on a separate GPU server
(see [model_server/README.md](model_server/README.md)).

**Status: scaffold only.** Only `/health` and `/ready` work so far. There is no call logic or AI
logic yet. See [docs/architecture.md](docs/architecture.md) for the layers.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check. Returns `{"status": "ok", "version": "..."}` with status 200. |
| GET | `/ready` | Readiness check. Tests Postgres (`SELECT 1`) and Redis (`PING`). Returns 200, or 503 with the failing check. |

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python 3.12 is downloaded by uv if missing)
- Docker with the Compose plugin (for the full stack)

## Run locally without Docker (app only)

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --reload --port 8000
```

Then:

```bash
curl http://127.0.0.1:8000/health
```

`/ready` returns 503 until Postgres and Redis are reachable at the URLs in `.env`. In `.env`,
change the hosts `postgres` and `redis` to `localhost` if you run them outside Compose.

## Run locally with Docker Compose (full stack)

```bash
cp .env.example .env
```

Edit `.env` and set:

- `DOMAIN=localhost`. Caddy then serves HTTPS with its own local certificate.
- `POSTGRES_PASSWORD` to a value of your choice, and use the same password inside `DATABASE_URL`.

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
curl -k https://localhost/health
curl http://127.0.0.1:8000/ready
```

Logs and shutdown:

```bash
docker compose logs -f app
docker compose down
```

`docker compose down -v` also deletes the Postgres, Redis and Caddy data volumes.

## Tests, lint and type checks

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Install the git hooks once (they run ruff and mypy on commit):

```bash
uv run pre-commit install
```

## Deploy on the server (AWS t3.small, Ubuntu)

1. In AWS, open inbound ports 22, 80 and 443 (TCP) and 443 (UDP) in the instance's security group.
2. Point a DNS A record (for example `calls.example.com`) to the instance's public IP.
3. On the server, install Docker:

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER
   ```

   Log out and back in so the group change applies.
4. Get the code and configure it:

   ```bash
   git clone <your-repo-url> ai-calling-agent
   cd ai-calling-agent
   cp .env.example .env
   nano .env
   ```

   Set `DOMAIN` to your real domain, set `PUBLIC_BASE_URL=https://<your domain>`, set strong
   values for `POSTGRES_PASSWORD` (also inside `DATABASE_URL`) and `GATEWAY_API_KEY`, and set
   `APP_ENV=production`.
5. Start everything:

   ```bash
   docker compose up -d --build
   ```

6. Check that it works. Caddy fetches the certificate on first start, which can take a minute.

   ```bash
   docker compose ps
   curl https://<your domain>/health
   ```

To update after a `git pull`:

```bash
docker compose up -d --build
```

## Memory budget (t3.small, 2 GB)

| Service | Limit |
|---|---|
| app | 512 MB |
| postgres | 384 MB |
| redis | 128 MB (data capped at 96 MB) |
| caddy | 64 MB |

The app limit will need review once the voice pipeline runs real calls (Silero VAD and audio
buffers for each concurrent call).

## Configuration

All settings come from environment variables or `.env`. See [.env.example](.env.example).
`.env` is git-ignored. Never commit real secrets.
