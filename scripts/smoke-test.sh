#!/usr/bin/env bash
set -euo pipefail

composeFile="${COMPOSE_FILE:-compose.yaml}"
projectName="${COMPOSE_PROJECT_NAME:-knowhere-self-hosted-smoke}"
projectNameLocal="${COMPOSE_PROJECT_NAME_LOCAL:-knowhere-self-hosted-smoke-local}"

export DASHBOARD_HOST_PORT="${DASHBOARD_HOST_PORT:-13000}"
export API_HOST_PORT="${API_HOST_PORT:-15005}"
export POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-15432}"
export REDIS_HOST_PORT="${REDIS_HOST_PORT:-16379}"
export LOCALSTACK_HOST_PORT="${LOCALSTACK_HOST_PORT:-14566}"

dashboardUrl="${DASHBOARD_SMOKE_URL:-http://127.0.0.1:${DASHBOARD_HOST_PORT}/login}"
apiUrl="${API_SMOKE_URL:-http://127.0.0.1:${API_HOST_PORT}/health}"

apiHostBind="${API_HOST_BIND:-127.0.0.1}"
apiHostPort="${API_HOST_PORT:-15005}"
apiKeySmokeDeviceId="${SMOKE_DEVICE_ID:-smoke-test-fixture}"
smokeFixturePdf="${SMOKE_FIXTURE_PDF:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke-fixtures/sample.pdf}"
smokeE2ePython="${SMOKE_E2E_PYTHON:-python3}"

waitForEndpoints() {
  local url="$1"
  local name="$2"
  local attempts="${3:-90}"
  for attempt in $(seq 1 "${attempts}"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "${name} is ready"
      return 0
    fi
    echo "Waiting for ${name} (${attempt}/${attempts})..."
    sleep 2
  done
  echo "${name} did not become ready" >&2
  return 1
}

runStartupSmoke() {
  local project="$1"
  shift || true
  local extra_env=("$@")

  echo "=== Starting compose project: ${project} ==="
  if [ "${#extra_env[@]}" -gt 0 ]; then
    env "${extra_env[@]}" docker compose -p "$project" -f "$composeFile" up -d postgres redis localstack app
  else
    docker compose -p "$project" -f "$composeFile" up -d postgres redis localstack app
  fi

  if ! waitForEndpoints "$apiUrl" "Knowhere API" 90; then
    docker compose -p "$project" -f "$composeFile" logs --no-color app
    return 1
  fi
  if ! waitForEndpoints "$dashboardUrl" "Dashboard" 90; then
    docker compose -p "$project" -f "$composeFile" logs --no-color app
    return 1
  fi
  echo "Startup smoke passed for ${project}"
}

runE2eParse() {
  if [ "${SMOKE_E2E:-false}" != "true" ]; then
    echo "SMOKE_E2E!=true; skipping end-to-end parse test"
    return 0
  fi

  if [ ! -f "$smokeFixturePdf" ]; then
    echo "Smoke fixture PDF not found at ${smokeFixturePdf}" >&2
    return 1
  fi

  local apiBaseUrl="http://${apiHostBind}:${apiHostPort}/api"
  echo "Registering guest device for E2E parse test"
  local registerResponse
  registerResponse=$(curl -fsS -X POST "${apiBaseUrl}/v1/guest" \
    -H 'Content-Type: application/json' \
    -d "{\"device_id\":\"${apiKeySmokeDeviceId}\",\"client\":\"smoke-script\",\"platform\":\"linux\"}")

  local apiKey
  apiKey=$(printf '%s' "$registerResponse" | "${smokeE2ePython}" -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
  if [ -z "$apiKey" ]; then
    echo "Failed to extract api_key from guest registration response" >&2
    return 1
  fi
  echo "Got API key: ${apiKey:0:12}..."

  echo "Submitting ${smokeFixturePdf} via Knowhere SDK"
  local script
  script="$(dirname "${BASH_SOURCE[0]}")/smoke-e2e-parse.py"
  if [ ! -f "$script" ]; then
    echo "E2E parse helper not found at ${script}" >&2
    return 1
  fi
  SMOKE_API_KEY="$apiKey" SMOKE_API_BASE_URL="$apiBaseUrl" SMOKE_PDF="$smokeFixturePdf" \
    "${smokeE2ePython}" "$script"
}

cleanup() {
  local project="$1"
  docker compose -p "$project" -f "$composeFile" down --remove-orphans >/dev/null 2>&1 || true
}

trap 'cleanup "${projectName}"; cleanup "${projectNameLocal}"' EXIT

runStartupSmoke "$projectName"
echo "=== Startup smoke (default config) passed ==="

echo ""
echo "=== Running local-mode env-wiring smoke (unreachable MINERU_URL) ==="
localDashboardPort="${LOCAL_DASHBOARD_HOST_PORT:-13100}"
localApiPort="${LOCAL_API_HOST_PORT:-15105}"
localPostgresPort="${LOCAL_POSTGRES_HOST_PORT:-15433}"
localRedisPort="${LOCAL_REDIS_HOST_PORT:-16380}"
localLocalstackPort="${LOCAL_LOCALSTACK_HOST_PORT:-14567}"

MINERU_LOCAL_MODE=true \
MINERU_URL=http://invalid-local-mineru.example:9999 \
DASHBOARD_HOST_PORT="${localDashboardPort}" \
API_HOST_PORT="${localApiPort}" \
POSTGRES_HOST_PORT="${localPostgresPort}" \
REDIS_HOST_PORT="${localRedisPort}" \
LOCALSTACK_HOST_PORT="${localLocalstackPort}" \
  docker compose -p "$projectNameLocal" -f "$composeFile" up -d postgres redis localstack app

localApiUrl="http://127.0.0.1:${localApiPort}/health"
localDashboardUrl="http://127.0.0.1:${localDashboardPort}/login"
if ! waitForEndpoints "$localApiUrl" "Knowhere API (local mode)" 90; then
  docker compose -p "$projectNameLocal" -f "$composeFile" logs --no-color app
  exit 1
fi
if ! waitForEndpoints "$localDashboardUrl" "Dashboard (local mode)" 90; then
  docker compose -p "$projectNameLocal" -f "$composeFile" logs --no-color app
  exit 1
fi
echo "Local-mode env-wiring smoke passed"

if [ "${SMOKE_E2E:-false}" = "true" ]; then
  echo ""
  echo "=== Running end-to-end parse smoke (requires reachable MinerU + LLM keys) ==="
  if ! runE2eParse; then
    echo "E2E parse test failed" >&2
    exit 1
  fi
fi

echo ""
echo "All smoke tests passed"
