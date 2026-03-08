#!/usr/bin/env bash
set -euo pipefail

# Upsert the recommended BUY entry quality guard values into the active .env file.
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

upsert_key() {
  local file="$1"
  local key="$2"
  local value="$3"

  if command -v rg >/dev/null 2>&1; then
    if rg -q "^${key}=" "$file"; then
      sed -i "s|^${key}=.*|${key}=${value}|g" "$file"
    else
      echo "${key}=${value}" >> "$file"
    fi
  else
    if grep -qE "^${key}=" "$file"; then
      sed -i "s|^${key}=.*|${key}=${value}|g" "$file"
    else
      echo "${key}=${value}" >> "$file"
    fi
  fi
}

print_target_values() {
  cat <<'EOF'
ENABLE_BREAKOUT_EXTENSION_CAP=true
MAX_BREAKOUT_EXTENSION_PCT_ETF=0.004
MAX_BREAKOUT_EXTENSION_PCT_STOCK=0.007
ENABLE_ENTRY_GAP_FILTER=true
MAX_ENTRY_GAP_PCT_ETF=0.01
MAX_ENTRY_GAP_PCT_STOCK=0.015
MAX_OPEN_VS_PREV_HIGH_PCT=0.005
ENABLE_OPENING_NO_ENTRY_GUARD=true
OPENING_NO_ENTRY_MINUTES=10
ENTRY_ORDER_STYLE=protected_limit
ENTRY_PROTECT_TICKS_ETF=1
ENTRY_PROTECT_TICKS_STOCK=2
ENTRY_MAX_SLIPPAGE_PCT=0.004
ENABLE_STALE_QUOTE_GUARD=true
QUOTE_MAX_AGE_SEC=3
EOF
}

TARGET_ENV_FILE="$(resolve_env_file)"

if [[ ! -f "${TARGET_ENV_FILE}" ]]; then
  echo "[ERROR] target env file not found: ${TARGET_ENV_FILE}" >&2
  exit 1
fi

echo "[INFO] target_env=${TARGET_ENV_FILE}"

if (( DRY_RUN == 1 )); then
  echo "[DRY-RUN] would apply:"
  print_target_values
  exit 0
fi

BACKUP_FILE="${TARGET_ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
cp "${TARGET_ENV_FILE}" "${BACKUP_FILE}"
echo "[OK] backup created: ${BACKUP_FILE}"

upsert_key "${TARGET_ENV_FILE}" "ENABLE_BREAKOUT_EXTENSION_CAP" "true"
upsert_key "${TARGET_ENV_FILE}" "MAX_BREAKOUT_EXTENSION_PCT_ETF" "0.004"
upsert_key "${TARGET_ENV_FILE}" "MAX_BREAKOUT_EXTENSION_PCT_STOCK" "0.007"
upsert_key "${TARGET_ENV_FILE}" "ENABLE_ENTRY_GAP_FILTER" "true"
upsert_key "${TARGET_ENV_FILE}" "MAX_ENTRY_GAP_PCT_ETF" "0.01"
upsert_key "${TARGET_ENV_FILE}" "MAX_ENTRY_GAP_PCT_STOCK" "0.015"
upsert_key "${TARGET_ENV_FILE}" "MAX_OPEN_VS_PREV_HIGH_PCT" "0.005"
upsert_key "${TARGET_ENV_FILE}" "ENABLE_OPENING_NO_ENTRY_GUARD" "true"
upsert_key "${TARGET_ENV_FILE}" "OPENING_NO_ENTRY_MINUTES" "10"
upsert_key "${TARGET_ENV_FILE}" "ENTRY_ORDER_STYLE" "protected_limit"
upsert_key "${TARGET_ENV_FILE}" "ENTRY_PROTECT_TICKS_ETF" "1"
upsert_key "${TARGET_ENV_FILE}" "ENTRY_PROTECT_TICKS_STOCK" "2"
upsert_key "${TARGET_ENV_FILE}" "ENTRY_MAX_SLIPPAGE_PCT" "0.004"
upsert_key "${TARGET_ENV_FILE}" "ENABLE_STALE_QUOTE_GUARD" "true"
upsert_key "${TARGET_ENV_FILE}" "QUOTE_MAX_AGE_SEC" "3"

echo "[OK] applied BUY entry quality profile:"
print_target_values
