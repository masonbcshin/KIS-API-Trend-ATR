#!/usr/bin/env bash
set -euo pipefail

# Upsert the recommended BUY entry quality guard values into the active .env file.
# The script maintains one managed block with comments and values.
#
# Usage:
#   tools/migrate_entry_buy_quality_env.sh
#   tools/migrate_entry_buy_quality_env.sh --env-file /path/to/.env
#   tools/migrate_entry_buy_quality_env.sh --dry-run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PACKAGE_ENV_FILE="${REPO_ROOT}/kis_trend_atr_trading/.env"
ROOT_ENV_FILE="${REPO_ROOT}/.env"

ENV_FILE=""
DRY_RUN=0
MANAGED_BLOCK_START="# BEGIN MANAGED BUY ENTRY QUALITY SETTINGS"
MANAGED_BLOCK_END="# END MANAGED BUY ENTRY QUALITY SETTINGS"
MANAGED_KEYS=(
  ENABLE_BREAKOUT_EXTENSION_CAP
  MAX_BREAKOUT_EXTENSION_PCT_ETF
  MAX_BREAKOUT_EXTENSION_PCT_STOCK
  BREAKOUT_EXTENSION_OPENING_CAP_MINUTES
  MAX_BREAKOUT_EXTENSION_PCT_ETF_OPENING
  MAX_BREAKOUT_EXTENSION_PCT_STOCK_OPENING
  ENABLE_BREAKOUT_EXTENSION_ATR_CAP
  BREAKOUT_EXTENSION_ATR_MULTIPLIER
  MAX_BREAKOUT_EXTENSION_PCT_ETF_HARD
  MAX_BREAKOUT_EXTENSION_PCT_STOCK_HARD
  ENABLE_ENTRY_GAP_FILTER
  MAX_ENTRY_GAP_PCT_ETF
  MAX_ENTRY_GAP_PCT_STOCK
  MAX_OPEN_VS_PREV_HIGH_PCT
  ENABLE_OPENING_NO_ENTRY_GUARD
  OPENING_NO_ENTRY_MINUTES
  ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY
  ORB_OPENING_RANGE_MINUTES
  ORB_ENTRY_START_MINUTES
  ORB_ENTRY_CUTOFF_MINUTES
  ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT
  ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF
  ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK
  ORB_MAX_EXTENSION_PCT_ETF
  ORB_MAX_EXTENSION_PCT_STOCK
  ORB_REQUIRE_ABOVE_VWAP
  ORB_USE_ADX_FILTER
  ORB_MIN_ADX
  ORB_RECENT_BREAKOUT_LOOKBACK_BARS
  ORB_REARM_BAND_PCT
  ORB_BLOCK_IF_PENDING_ORDER
  ORB_ONLY_MAIN_MARKET
  ORB_ALLOWED_ENTRY_VENUES
  ENTRY_ORDER_STYLE
  ENTRY_PROTECT_TICKS_ETF
  ENTRY_PROTECT_TICKS_STOCK
  ENTRY_MAX_SLIPPAGE_PCT
  ENABLE_STALE_QUOTE_GUARD
  QUOTE_MAX_AGE_SEC
)

usage() {
  cat <<'USAGE'
Usage:
  migrate_entry_buy_quality_env.sh [options]

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
# 신규 BUY 품질 보호 설정.
# managed by tools/migrate_entry_buy_quality_env.sh
#
# 돌파 확장폭 상한:
#   prev_high 대비 현재가 확장폭이 과도하면 신규 BUY를 차단합니다.
#   장초에는 opening cap, 변동성이 큰 종목에는 ATR 비례 cap으로 완화하되
#   hard cap으로 최종 추격 상한을 둡니다.
ENABLE_BREAKOUT_EXTENSION_CAP=true
MAX_BREAKOUT_EXTENSION_PCT_ETF=0.004
MAX_BREAKOUT_EXTENSION_PCT_STOCK=0.007
BREAKOUT_EXTENSION_OPENING_CAP_MINUTES=90
MAX_BREAKOUT_EXTENSION_PCT_ETF_OPENING=0.012
MAX_BREAKOUT_EXTENSION_PCT_STOCK_OPENING=0.018
ENABLE_BREAKOUT_EXTENSION_ATR_CAP=true
BREAKOUT_EXTENSION_ATR_MULTIPLIER=0.35
MAX_BREAKOUT_EXTENSION_PCT_ETF_HARD=0.02
MAX_BREAKOUT_EXTENSION_PCT_STOCK_HARD=0.035
#
# 장초 갭 과열 차단:
#   시가가 전일 종가 대비 과도하게 갭상승했거나,
#   시가가 prev_high를 의미 있게 상회하면 신규 BUY를 차단합니다.
ENABLE_ENTRY_GAP_FILTER=true
MAX_ENTRY_GAP_PCT_ETF=0.01
MAX_ENTRY_GAP_PCT_STOCK=0.015
MAX_OPEN_VS_PREV_HIGH_PCT=0.005
#
# 장 시작 직후 신규 BUY 금지:
#   정규장 시작 후 지정 분 동안 신규 BUY만 차단합니다.
ENABLE_OPENING_NO_ENTRY_GUARD=true
OPENING_NO_ENTRY_MINUTES=10
#
# Opening Range Breakout (ORB) 보조 진입:
#   장초 갭으로 prev_high 기반 cap/gap 필터를 넘겨버린 종목은
#   opening range 재돌파 + VWAP 확인 시에만 제한적으로 허용합니다.
ENABLE_OPENING_RANGE_BREAKOUT_STRATEGY=true
ORB_OPENING_RANGE_MINUTES=5
ORB_ENTRY_START_MINUTES=0
ORB_ENTRY_CUTOFF_MINUTES=90
ORB_MIN_OPEN_ABOVE_PREV_HIGH_PCT=0.003
ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_ETF=0.05
ORB_MAX_OPEN_ABOVE_PREV_HIGH_PCT_STOCK=0.10
ORB_MAX_EXTENSION_PCT_ETF=0.006
ORB_MAX_EXTENSION_PCT_STOCK=0.01
ORB_REQUIRE_ABOVE_VWAP=true
ORB_USE_ADX_FILTER=true
ORB_MIN_ADX=20
ORB_RECENT_BREAKOUT_LOOKBACK_BARS=3
ORB_REARM_BAND_PCT=0.002
ORB_BLOCK_IF_PENDING_ORDER=true
ORB_ONLY_MAIN_MARKET=true
ORB_ALLOWED_ENTRY_VENUES=KRX
#
# 신규 BUY 주문 방식:
#   시장가 대신 보호형 지정가를 사용합니다.
ENTRY_ORDER_STYLE=protected_limit
ENTRY_PROTECT_TICKS_ETF=1
ENTRY_PROTECT_TICKS_STOCK=2
ENTRY_MAX_SLIPPAGE_PCT=0.004
#
# stale quote 차단:
#   WS quote/tick이 오래됐으면 신규 BUY를 차단합니다.
ENABLE_STALE_QUOTE_GUARD=true
QUOTE_MAX_AGE_SEC=3
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

echo "[OK] applied BUY entry quality profile with comments:"
build_managed_block
