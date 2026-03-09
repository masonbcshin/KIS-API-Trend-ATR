#!/usr/bin/env bash
set -euo pipefail

# Upsert the recommended market regime filter values into the active .env file.
# The script maintains one managed block with comments and values.
#
# Usage:
#   tools/migrate_market_regime_env.sh
#   tools/migrate_market_regime_env.sh --env-file /path/to/.env
#   tools/migrate_market_regime_env.sh --dry-run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PACKAGE_ENV_FILE="${REPO_ROOT}/kis_trend_atr_trading/.env"
ROOT_ENV_FILE="${REPO_ROOT}/.env"

ENV_FILE=""
DRY_RUN=0
MANAGED_BLOCK_START="# BEGIN MANAGED MARKET REGIME SETTINGS"
MANAGED_BLOCK_END="# END MANAGED MARKET REGIME SETTINGS"
MANAGED_KEYS=(
  ENABLE_MARKET_REGIME_FILTER
  MARKET_REGIME_KOSPI_SYMBOL
  MARKET_REGIME_KOSDAQ_SYMBOL
  MARKET_REGIME_MA_PERIOD
  MARKET_REGIME_LOOKBACK_DAYS
  MARKET_REGIME_BAD_3D_RETURN_PCT
  MARKET_REGIME_INTRADAY_DROP_PCT
  MARKET_REGIME_OPENING_GUARD_MINUTES
  MARKET_REGIME_CACHE_TTL_SEC
  MARKET_REGIME_OPENING_CACHE_TTL_SEC
  MARKET_REGIME_STALE_MAX_SEC
  MARKET_REGIME_FAIL_MODE
  MARKET_REGIME_REFRESH_BUDGET_SEC
  MARKET_REGIME_BAD_BLOCK_NEW_BUY
  MARKET_REGIME_NEUTRAL_ALLOW_BUY
  MARKET_REGIME_NEUTRAL_POSITION_SCALE
)

usage() {
  cat <<'USAGE'
Usage:
  migrate_market_regime_env.sh [options]

Options:
  --env-file <path>  Explicit target .env file
  --dry-run          Print the target values only
  -h, --help         Show this help
USAGE
}

while (( "$#" )); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

resolve_env_file() {
  if [[ -n "${ENV_FILE}" ]]; then
    echo "${ENV_FILE}"
    return
  fi

  if [[ -f "${PACKAGE_ENV_FILE}" ]]; then
    echo "${PACKAGE_ENV_FILE}"
    return
  fi

  if [[ -f "${ROOT_ENV_FILE}" ]]; then
    echo "${ROOT_ENV_FILE}"
    return
  fi

  echo "[ERROR] no .env file found. checked:" >&2
  echo "  - ${PACKAGE_ENV_FILE}" >&2
  echo "  - ${ROOT_ENV_FILE}" >&2
  exit 1
}

build_managed_block() {
  cat <<EOF
${MANAGED_BLOCK_START}
# 시장 레짐 필터 권장값.
# managed by tools/migrate_market_regime_env.sh
#
# 레짐 필터 기본 활성화:
#   대표 ETF 2개(코스피/코스닥) 기반으로 신규 BUY 상위 필터를 적용합니다.
ENABLE_MARKET_REGIME_FILTER=true
MARKET_REGIME_KOSPI_SYMBOL=069500
MARKET_REGIME_KOSDAQ_SYMBOL=229200
MARKET_REGIME_MA_PERIOD=20
MARKET_REGIME_LOOKBACK_DAYS=3
MARKET_REGIME_BAD_3D_RETURN_PCT=-0.03
#
# 장초 급락 가드:
#   정규장 초반 대표 ETF가 시가 대비 급락하면 레짐을 BAD로 강등합니다.
MARKET_REGIME_INTRADAY_DROP_PCT=-0.015
MARKET_REGIME_OPENING_GUARD_MINUTES=30
#
# shared snapshot TTL:
#   v1 기본값은 일반장/장초 모두 60초로 유지합니다.
MARKET_REGIME_CACHE_TTL_SEC=60
MARKET_REGIME_OPENING_CACHE_TTL_SEC=60
MARKET_REGIME_STALE_MAX_SEC=180
#
# stale/failure 정책:
#   snapshot stale 또는 refresh 실패 시 신규 BUY를 fail-closed로 차단합니다.
MARKET_REGIME_FAIL_MODE=closed
MARKET_REGIME_REFRESH_BUDGET_SEC=1.5
#
# BUY 정책:
#   BAD는 차단, NEUTRAL은 허용, 포지션 스케일은 현재 1.0(로그/향후 확장용)입니다.
MARKET_REGIME_BAD_BLOCK_NEW_BUY=true
MARKET_REGIME_NEUTRAL_ALLOW_BUY=true
MARKET_REGIME_NEUTRAL_POSITION_SCALE=1.0
${MANAGED_BLOCK_END}
EOF
}

write_managed_block() {
  local file="$1"
  local tmp_file
  local key_pattern
  tmp_file="$(mktemp)"
  key_pattern="^($(IFS='|'; echo "${MANAGED_KEYS[*]}"))="

  awk -v start="${MANAGED_BLOCK_START}" -v end="${MANAGED_BLOCK_END}" '
    $0 == start { skip = 1; next }
    $0 == end { skip = 0; next }
    skip != 1 { print }
  ' "${file}" > "${tmp_file}"

  if command -v rg >/dev/null 2>&1; then
    rg -v "${key_pattern}" "${tmp_file}" > "${tmp_file}.clean" || true
  else
    grep -Ev "${key_pattern}" "${tmp_file}" > "${tmp_file}.clean" || true
  fi

  mv "${tmp_file}.clean" "${tmp_file}"

  {
    cat "${tmp_file}"
    echo
    build_managed_block
    echo
  } > "${file}"

  rm -f "${tmp_file}"
}

TARGET_ENV_FILE="$(resolve_env_file)"

if [[ ! -f "${TARGET_ENV_FILE}" ]]; then
  echo "[ERROR] target env file not found: ${TARGET_ENV_FILE}" >&2
  exit 1
fi

echo "[INFO] target_env=${TARGET_ENV_FILE}"

if (( DRY_RUN == 1 )); then
  echo "[DRY-RUN] would apply managed block:"
  build_managed_block
  exit 0
fi

BACKUP_FILE="${TARGET_ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
cp "${TARGET_ENV_FILE}" "${BACKUP_FILE}"
echo "[OK] backup created: ${BACKUP_FILE}"

write_managed_block "${TARGET_ENV_FILE}"

echo "[OK] applied market regime profile with comments:"
build_managed_block
