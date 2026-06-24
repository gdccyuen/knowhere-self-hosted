#!/usr/bin/env bash
set -Eeuo pipefail

isEnabled() {
  local value="${1:-}"

  case "${value,,}" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

checkUrl() {
  local url="$1"

  curl -fsS "$url" >/dev/null
}

apiPort="${API_PORT:-5005}"
dashboardPort="${DASHBOARD_PORT:-3000}"

checkUrl "http://127.0.0.1:${apiPort}/health"

if isEnabled "${SELF_HOSTED_START_DASHBOARD:-true}"; then
  checkUrl "http://127.0.0.1:${dashboardPort}/login"
fi
