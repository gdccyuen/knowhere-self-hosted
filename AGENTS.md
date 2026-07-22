# AGENTS.md

## What this repo is

Self-hosted deployment packaging for [Knowhere](https://github.com/Ontos-AI/knowhere) — a document ingestion, retrieval, and MCP backend. This repo contains only the deployment glue; the application source lives in two sibling repos (`knowhere` and `knowhere-dashboard`) that are **not** checked in here.

## Architecture

Single Docker image runs three processes via `scripts/entrypoint.sh` (supervised by `tini`):
- **API** (Python/FastAPI) — port 5005, venv at `/opt/knowhere/venvs/api`
- **Worker** (Python/Celery+gevent) — same source tree, venv at `/opt/knowhere/venvs/worker`
- **Dashboard** (Next.js/Node) — port 3000, standalone build at `/opt/knowhere/dashboard`

Backing services in `compose.yaml`: PostgreSQL 15, Redis 7, LocalStack 3.8 (S3/SNS/SQS).

## Build flow

1. `scripts/prepare-sources.sh` copies source from sibling checkouts into `.build/sources/` (defaults to `../knowhere` and `../knowhere-dashboard`).
2. `Dockerfile` multi-stage build installs Python deps with `uv sync --locked`, installs Node deps with `pnpm install --frozen-lockfile`, builds the Next.js dashboard, then assembles a single runner image.
3. Build context requires `.build/sources/` to exist — you must run `prepare-sources.sh` before `docker build`.

## Startup sequence (entrypoint.sh)

1. Generate or load secrets into `/data/secrets/` volume
2. Wait for PostgreSQL → ensure `uuid-ossp` and `pg_trgm` extensions
3. Wait for Redis
4. Create LocalStack S3 buckets
5. Run Dashboard migrations (`drizzle-kit migrate`)
6. Start API → wait for `/health`
7. Configure S3 storage events (SNS/SQS wiring)
8. Start Worker, then Dashboard

## Key commands

```bash
# Start the stack
docker compose up -d

# View logs
docker compose logs -f app

# Stop
docker compose down

# Update and restart
docker compose pull && docker compose up -d

# Smoke test (uses non-default ports to avoid conflicts)
bash scripts/smoke-test.sh

# Build locally (requires sibling repos checked out)
bash scripts/prepare-sources.sh
docker build .
```

## Environment configuration

- `.env.defaults` — built-in defaults, loaded first by Compose. Do not edit.
- `.env` — user overrides, loaded second. Only put values you need to override. Never committed (gitignored).
- Required for a working deployment: `MINERU_API_KEYS` + either `DS_KEY` or `ALI_API_KEYS`.
- Ports bind to `127.0.0.1` by default. Set `*_HOST_BIND=0.0.0.0` for external access.
- `DASHBOARD_PUBLIC_URL` must match the browser URL or login/signup will fail.
- Secrets (`SECRET_KEY`, `BETTER_AUTH_SECRET`, etc.) auto-generate on first start and persist in the `knowhere_secrets` Docker volume.
- API and Dashboard use separate database URL formats: `API_DATABASE_URL` uses `postgresql+asyncpg://`, `DASHBOARD_DATABASE_URL` uses `postgresql://`.

## Important env var quirks

- `NORMOL_MODEL` — not a typo; this is the actual variable name for the main text model.
- `S3_ENDPOINT_URL` inside the container resolves to `http://localstack:4566`, but in `.env.defaults` it shows `http://localhost.localstack.cloud:4566` for host-side tooling. `entrypoint.sh` overrides the container-internal default.
- `MINERU_API_KEYS` and `ALI_API_KEYS` support comma-separated key pools for rotation.

## What is NOT in this repo

- Application Python code (`apps/api/`, `apps/worker/`, `packages/shared-python/`) — lives in the `knowhere` monorepo
- Dashboard Next.js code — lives in the `knowhere-dashboard` repo
- Tests — there are none in this repo; testing is done through `scripts/smoke-test.sh`
- CI workflows — directory exists but is empty
