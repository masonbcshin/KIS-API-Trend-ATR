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

PROFILE_KEYS=(
  EXECUTION_MODE
  TRADING_MODE
  ENABLE_REAL_TRADING
  TREND_MA_PERIOD
  ADX_THRESHOLD
  ATR_SPIKE_THRESHOLD
  ATR_PERIOD
  ADX_PERIOD
  DATA_FEED_DEFAULT
)

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

key_description() {
  local key="$1"
  case "${key}" in
    EXECUTION_MODE)
      echo "실행 안전 모드. REAL이면 실거래 설정 파일(settings_real.py) 로드."
      ;;
    TRADING_MODE)
      echo "주문/계좌 네임스페이스 모드. REAL이면 실계좌 경로를 사용."
      ;;
    ENABLE_REAL_TRADING)
      echo "실거래 이중 승인 스위치. true여야 REAL 주문이 실제로 허용됨."
      ;;
    TREND_MA_PERIOD)
      echo "추세 판단 이동평균 기간(일). 작을수록 추세 전환을 더 빠르게 반영."
      ;;
    ADX_THRESHOLD)
      echo "진입 허용 최소 추세강도(ADX). 낮출수록 진입이 늘고, 높일수록 엄격해짐."
      ;;
    ATR_SPIKE_THRESHOLD)
      echo "ATR 급등 차단 임계 배수. 값이 낮을수록 변동성 급등 구간 진입을 더 보수적으로 차단."
      ;;
    ATR_PERIOD)
      echo "ATR 계산 기간(일). 짧을수록 최근 변동성 반영이 빠름."
      ;;
    ADX_PERIOD)
      echo "ADX 계산 기간(일). 짧을수록 추세강도 변화 반응이 빠름."
      ;;
    DATA_FEED_DEFAULT)
      echo "기본 시세 소스(rest|ws). ws면 장중 실시간 틱 우선 사용."
      ;;
    *)
      echo "설명 없음"
      ;;
  esac
}

read_env_value() {
  local file="$1"
  local key="$2"
  local line
  line="$(grep -E "^${key}=" "${file}" | tail -n1 || true)"
  if [[ -z "${line}" ]]; then
    echo "<unset>"
    return
  fi
  echo "${line#*=}"
}

print_profile_with_descriptions() {
  local file="$1"
  echo "[INFO] active keys with descriptions:"
  for key in "${PROFILE_KEYS[@]}"; do
    local value
    value="$(read_env_value "${file}" "${key}")"
    printf "  - %s=%s\n" "${key}" "${value}"
    printf "    %s\n" "$(key_description "${key}")"
  done
}

print_recommended_profile_with_descriptions() {
  echo "[INFO] recommended REAL profile keys:"
  printf "  - EXECUTION_MODE=REAL\n    %s\n" "$(key_description EXECUTION_MODE)"
  printf "  - TRADING_MODE=REAL\n    %s\n" "$(key_description TRADING_MODE)"
  printf "  - ENABLE_REAL_TRADING=true\n    %s\n" "$(key_description ENABLE_REAL_TRADING)"
  printf "  - TREND_MA_PERIOD=35\n    %s\n" "$(key_description TREND_MA_PERIOD)"
  printf "  - ADX_THRESHOLD=22\n    %s\n" "$(key_description ADX_THRESHOLD)"
  printf "  - ATR_SPIKE_THRESHOLD=3.0\n    %s\n" "$(key_description ATR_SPIKE_THRESHOLD)"
  printf "  - ATR_PERIOD=14\n    %s\n" "$(key_description ATR_PERIOD)"
  printf "  - ADX_PERIOD=14\n    %s\n" "$(key_description ADX_PERIOD)"
  printf "  - DATA_FEED_DEFAULT=ws\n    %s\n" "$(key_description DATA_FEED_DEFAULT)"
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
  print_profile_with_descriptions "${ENV_FILE}"
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
  print_recommended_profile_with_descriptions
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
