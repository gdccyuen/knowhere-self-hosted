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

The repo supports two build paths:

### Full source build (upstream style)
1. `scripts/prepare-sources.sh` copies source from sibling checkouts into `.build/sources/` (defaults to `../knowhere` and `../knowhere-dashboard`).
2. `Dockerfile` multi-stage build installs Python deps with `uv sync --locked`, installs Node deps with `pnpm install --frozen-lockfile`, builds the Next.js dashboard, then assembles a single runner image.
3. Build context requires `.build/sources/` to exist — you must run `prepare-sources.sh` before `docker build`.

### Forked image build (local MinerU mode)
1. `Dockerfile.forked` starts `FROM ghcr.io/ontos-ai/knowhere:${KNOWHERE_BASE_TAG}` (default `v0.1.6`) — no sibling repos required.
2. Copies `patches/pdf_service.patch` and applies it with `patch -p1` against the upstream file inside the image, enabling `MINERU_LOCAL_MODE=true`.
3. Build with `docker compose build` (compose.yaml wires `KNOWHERE_BASE_TAG` through as a build arg) or `docker build -f Dockerfile.forked .`.
4. Verify the patch still applies against a new base tag before bumping: `bash scripts/check-patch-drift.sh`.
5. Edit the patch: `bash scripts/edit-patch.sh` (extracts upstream, applies current patch, opens `$EDITOR`, regenerates diff on exit).

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

# Build the forked image (local MinerU mode)
docker compose build

# Verify the forked-image patch still applies to the configured base tag
bash scripts/check-patch-drift.sh

# Smoke test with end-to-end parse (requires reachable MinerU + LLM keys)
SMOKE_E2E=true bash scripts/smoke-test.sh
```

## Environment configuration

- `.env.defaults` — built-in defaults, loaded first by Compose. Do not edit.
- `.env` — user overrides, loaded second. Only put values you need to override. Never committed (gitignored).
- Required for a working deployment: `MINERU_API_KEYS` + either `DS_KEY` or `ALI_API_KEYS`. When `MINERU_LOCAL_MODE=true`, `MINERU_API_KEYS` is not required.
- Ports bind to `127.0.0.1` by default. Set `*_HOST_BIND=0.0.0.0` for external access.
- `DASHBOARD_PUBLIC_URL` must match the browser URL or login/signup will fail.
- Secrets (`SECRET_KEY`, `BETTER_AUTH_SECRET`, etc.) auto-generate on first start and persist in the `knowhere_secrets` Docker volume.
- API and Dashboard use separate database URL formats: `API_DATABASE_URL` uses `postgresql+asyncpg://`, `DASHBOARD_DATABASE_URL` uses `postgresql://`.

## Important env var quirks

- `NORMOL_MODEL` — not a typo; this is the actual variable name for the main text model.
- `S3_ENDPOINT_URL` inside the container resolves to `http://localstack:4566`, but in `.env.defaults` it shows `http://localhost.localstack.cloud:4566` for host-side tooling. `entrypoint.sh` overrides the container-internal default.
- `MINERU_API_KEYS` and `ALI_API_KEYS` support comma-separated key pools for rotation.
- `MINERU_LOCAL_MODE=true` bypasses `MINERU_API_KEYS` entirely; PDF parsing is routed to a self-hosted MinerU `/file_parse` endpoint via `MINERU_URL`. The patch lives at `patches/pdf_service.patch`; see `docs/adr/0001-local-mineru-mode.md` for rationale.
- `MINERU_LOCAL_LANG_LIST` does **not** accept `auto` — local MinerU's enum is `ch, ch_server, korean, ta, te, ka, th, el, arabic, east_slavic, cyrillic, devanagari`. Cloud `auto` is incompatible.
- `MINERU_IMAGE_MODEL`/`IMAGE_MODEL_MAX` must be vision-capable. Text-only models (e.g. `deepseek-v4-flash`) fail when Knowhere sends `image_url` content blocks.

## What is NOT in this repo

- Application Python code (`apps/api/`, `apps/worker/`, `packages/shared-python/`) — lives in the `knowhere` monorepo. The forked-image patch (`patches/pdf_service.patch`) is a unified diff against this upstream file; it is not the source itself.
- Dashboard Next.js code — lives in the `knowhere-dashboard` repo
- Tests — there are none in this repo; testing is done through `scripts/smoke-test.sh` (default startup smoke) or `SMOKE_E2E=true bash scripts/smoke-test.sh` (full parse against a live MinerU)
- CI workflows — `publish-image.yml` builds the upstream image from sibling repos; `publish-forked-image.yml` builds and publishes `ghcr.io/gdccyuen/knowhere-self-hosted:<tag>` from `Dockerfile.forked`
