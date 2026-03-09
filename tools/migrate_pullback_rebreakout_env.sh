#!/usr/bin/env bash
set -euo pipefail

# Upsert the recommended Pullback / Re-breakout strategy values into the active .env file.
# The script maintains one managed block with comments and values.
#
# Usage:
#   tools/migrate_pullback_rebreakout_env.sh
#   tools/migrate_pullback_rebreakout_env.sh --env-file /path/to/.env
#   tools/migrate_pullback_rebreakout_env.sh --dry-run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PACKAGE_ENV_FILE="${REPO_ROOT}/kis_trend_atr_trading/.env"
ROOT_ENV_FILE="${REPO_ROOT}/.env"

ENV_FILE=""
DRY_RUN=0
MANAGED_BLOCK_START="# BEGIN MANAGED PULLBACK REBREAKOUT SETTINGS"
MANAGED_BLOCK_END="# END MANAGED PULLBACK REBREAKOUT SETTINGS"
MANAGED_KEYS=(
  ENABLE_PULLBACK_REBREAKOUT_STRATEGY
  PULLBACK_LOOKBACK_BARS
  PULLBACK_SWING_LOOKBACK_BARS
  PULLBACK_MIN_PULLBACK_PCT
  PULLBACK_MAX_PULLBACK_PCT
  PULLBACK_REQUIRE_ABOVE_MA20
  PULLBACK_REBREAKOUT_LOOKBACK_BARS
  PULLBACK_USE_ADX_FILTER
  PULLBACK_MIN_ADX
  PULLBACK_ONLY_MAIN_MARKET
  PULLBACK_ALLOWED_ENTRY_VENUES
  PULLBACK_BLOCK_IF_EXISTING_POSITION
  PULLBACK_BLOCK_IF_PENDING_ORDER
)

usage() {
  cat <<'USAGE'
Usage:
  migrate_pullback_rebreakout_env.sh [options]

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
# Pullback / Re-breakout 보조 진입 슬리브 설정.
# managed by tools/migrate_pullback_rebreakout_env.sh
#
# 기본:
#   기존 Trend-ATR 본체는 그대로 두고, 건강한 눌림 후 재돌파 엔트리만 추가합니다.
ENABLE_PULLBACK_REBREAKOUT_STRATEGY=true
PULLBACK_LOOKBACK_BARS=12
PULLBACK_SWING_LOOKBACK_BARS=15
PULLBACK_MIN_PULLBACK_PCT=0.015
PULLBACK_MAX_PULLBACK_PCT=0.06
PULLBACK_REQUIRE_ABOVE_MA20=true
PULLBACK_REBREAKOUT_LOOKBACK_BARS=3
PULLBACK_USE_ADX_FILTER=true
PULLBACK_MIN_ADX=20
#
# 시장 단계:
#   v1은 메인마켓만 허용하고, 실제 운영 venue는 KRX로 제한합니다.
PULLBACK_ONLY_MAIN_MARKET=true
PULLBACK_ALLOWED_ENTRY_VENUES=KRX
#
# 중복 매수 방지:
#   기존 포지션 또는 미종결 BUY 주문이 있으면 Pullback 신규 진입을 차단합니다.
PULLBACK_BLOCK_IF_EXISTING_POSITION=true
PULLBACK_BLOCK_IF_PENDING_ORDER=true
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

if (( DRY_RUN == 1 )); then
  if [[ -n "${ENV_FILE}" ]]; then
    echo "[INFO] target_env=${ENV_FILE}"
  elif [[ -f "${PACKAGE_ENV_FILE}" ]]; then
    echo "[INFO] target_env=${PACKAGE_ENV_FILE}"
  elif [[ -f "${ROOT_ENV_FILE}" ]]; then
    echo "[INFO] target_env=${ROOT_ENV_FILE}"
  else
    echo "[INFO] no .env file found, printing managed block only"
  fi
  echo "[DRY-RUN] would apply managed block:"
  build_managed_block
  exit 0
fi

TARGET_ENV_FILE="$(resolve_env_file)"

if [[ ! -f "${TARGET_ENV_FILE}" ]]; then
  echo "[ERROR] target env file not found: ${TARGET_ENV_FILE}" >&2
  exit 1
fi

echo "[INFO] target_env=${TARGET_ENV_FILE}"

BACKUP_FILE="${TARGET_ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
cp "${TARGET_ENV_FILE}" "${BACKUP_FILE}"
echo "[OK] backup created: ${BACKUP_FILE}"

write_managed_block "${TARGET_ENV_FILE}"

echo "[OK] applied Pullback / Re-breakout profile with comments:"
build_managed_block
