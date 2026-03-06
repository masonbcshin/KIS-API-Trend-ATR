#!/usr/bin/env bash
set -euo pipefail

# Apply recommended REAL profile settings and optionally restart auto-trade systemd service.
#
# Usage examples:
#   tools/deploy_recommended_real_profile.sh
#   tools/deploy_recommended_real_profile.sh --interval 30
#   tools/deploy_recommended_real_profile.sh --no-systemd
#   tools/deploy_recommended_real_profile.sh --dry-run

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE="${REPO_ROOT}/kis_trend_atr_trading/.env"
SERVICE_NAME="auto-trade.service"
PYTHON_BIN="/usr/bin/python3"
INTERVAL=30

DRY_RUN=0
USE_SYSTEMD=1
RESTART_SERVICE=1

usage() {
  cat <<'USAGE'
Usage:
  deploy_recommended_real_profile.sh [options]

Options:
  --env-file <path>      Target .env file path (default: kis_trend_atr_trading/.env)
  --service <name>       systemd service name (default: auto-trade.service)
  --python <path>        Python executable for ExecStart (default: /usr/bin/python3)
  --interval <seconds>   Runtime interval for main_multiday (default: 30)
  --no-systemd           Skip systemd override and restart
  --no-restart           Write override but do not restart service
  --dry-run              Print planned changes only
  -h, --help             Show this help
USAGE
}

while (( "$#" )); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --service)
      SERVICE_NAME="${2:-}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:-}"
      shift 2
      ;;
    --interval)
      INTERVAL="${2:-}"
      shift 2
      ;;
    --no-systemd)
      USE_SYSTEMD=0
      shift
      ;;
    --no-restart)
      RESTART_SERVICE=0
      shift
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
      echo "[ERROR] Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${ENV_FILE}" || -z "${SERVICE_NAME}" || -z "${PYTHON_BIN}" ]]; then
  echo "[ERROR] Invalid empty argument value" >&2
  exit 1
fi

if ! [[ "${INTERVAL}" =~ ^[0-9]+$ ]]; then
  echo "[ERROR] --interval must be an integer: ${INTERVAL}" >&2
  exit 1
fi

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

ensure_env_file() {
  if [[ -f "${ENV_FILE}" ]]; then
    return
  fi
  local example_file="${REPO_ROOT}/kis_trend_atr_trading/.env.example"
  if [[ ! -f "${example_file}" ]]; then
    echo "[ERROR] missing .env and .env.example: ${example_file}" >&2
    exit 1
  fi
  mkdir -p "$(dirname "${ENV_FILE}")"
  cp "${example_file}" "${ENV_FILE}"
  echo "[OK] created env from example: ${ENV_FILE}"
}

apply_env_profile() {
  local backup_file="${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
  cp "${ENV_FILE}" "${backup_file}"
  echo "[OK] env backup: ${backup_file}"

  upsert_key "${ENV_FILE}" "EXECUTION_MODE" "REAL"
  upsert_key "${ENV_FILE}" "TRADING_MODE" "REAL"
  upsert_key "${ENV_FILE}" "ENABLE_REAL_TRADING" "true"

  upsert_key "${ENV_FILE}" "TREND_MA_PERIOD" "35"
  upsert_key "${ENV_FILE}" "ADX_THRESHOLD" "22"
  upsert_key "${ENV_FILE}" "ATR_SPIKE_THRESHOLD" "3.0"
  upsert_key "${ENV_FILE}" "ATR_PERIOD" "14"
  upsert_key "${ENV_FILE}" "ADX_PERIOD" "14"
  upsert_key "${ENV_FILE}" "DATA_FEED_DEFAULT" "ws"

  echo "[OK] applied recommended REAL profile in ${ENV_FILE}"
  echo "[INFO] active keys:"
  grep -E '^(EXECUTION_MODE|TRADING_MODE|ENABLE_REAL_TRADING|TREND_MA_PERIOD|ADX_THRESHOLD|ATR_SPIKE_THRESHOLD|ATR_PERIOD|ADX_PERIOD|DATA_FEED_DEFAULT)=' "${ENV_FILE}" || true
}

write_systemd_override() {
  local override_dir="/etc/systemd/system/${SERVICE_NAME}.d"
  local override_file="${override_dir}/override.conf"
  local tmp_file
  tmp_file="$(mktemp)"

  cat > "${tmp_file}" <<EOF
[Service]
WorkingDirectory=${REPO_ROOT}
ExecStart=
ExecStart=${PYTHON_BIN} -m kis_trend_atr_trading.main_multiday --mode trade --confirm-real-trading --interval ${INTERVAL}
EOF

  echo "[INFO] writing systemd override: ${override_file}"
  sudo mkdir -p "${override_dir}"
  sudo tee "${override_file}" >/dev/null < "${tmp_file}"
  rm -f "${tmp_file}"

  sudo systemctl daemon-reload
  if (( RESTART_SERVICE == 1 )); then
    sudo systemctl restart "${SERVICE_NAME}"
    sudo systemctl status "${SERVICE_NAME}" --no-pager || true
  fi

  local checker="${REPO_ROOT}/tools/check_auto_trade_override.sh"
  if [[ -x "${checker}" ]]; then
    "${checker}" --service "${SERVICE_NAME}" --expected-interval "${INTERVAL}" --sudo || true
  fi
}

echo "[INFO] repo_root=${REPO_ROOT}"
echo "[INFO] env_file=${ENV_FILE}"
echo "[INFO] service=${SERVICE_NAME}"
echo "[INFO] interval=${INTERVAL}"
echo "[INFO] use_systemd=${USE_SYSTEMD}, restart_service=${RESTART_SERVICE}, dry_run=${DRY_RUN}"

if (( DRY_RUN == 1 )); then
  echo "[DRY-RUN] would apply recommended env keys and systemd override."
  exit 0
fi

ensure_env_file
apply_env_profile

if (( USE_SYSTEMD == 0 )); then
  echo "[INFO] --no-systemd enabled. skip service update."
  exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "[WARN] systemctl not available in this environment. env changes applied only."
  exit 0
fi

write_systemd_override
echo "[OK] deployment profile applied."
