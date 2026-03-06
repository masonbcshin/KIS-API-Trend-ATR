#!/usr/bin/env bash
set -euo pipefail

# Validate current auto-trade systemd unit / override status.
#
# Usage:
#   tools/check_auto_trade_override.sh
#   tools/check_auto_trade_override.sh --service auto-trade.service --expected-interval 30
#   tools/check_auto_trade_override.sh --sudo

SERVICE_NAME="auto-trade.service"
EXPECTED_INTERVAL=30
USE_SUDO=0
STRICT=0

usage() {
  cat <<'USAGE'
Usage:
  check_auto_trade_override.sh [options]

Options:
  --service <name>            systemd service name (default: auto-trade.service)
  --expected-interval <sec>   expected --interval value in ExecStart (default: 30)
  --sudo                      run systemctl commands via sudo
  --strict                    return non-zero when warnings are detected
  -h, --help                  show this help
USAGE
}

while (( "$#" )); do
  case "$1" in
    --service)
      SERVICE_NAME="${2:-}"
      shift 2
      ;;
    --expected-interval)
      EXPECTED_INTERVAL="${2:-}"
      shift 2
      ;;
    --sudo)
      USE_SUDO=1
      shift
      ;;
    --strict)
      STRICT=1
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

if ! [[ "${EXPECTED_INTERVAL}" =~ ^[0-9]+$ ]]; then
  echo "[ERROR] --expected-interval must be integer: ${EXPECTED_INTERVAL}" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "[WARN] systemctl not available in this environment."
  exit 0
fi

warnings=0
runctl() {
  if (( USE_SUDO == 1 )); then
    sudo systemctl "$@"
  else
    systemctl "$@"
  fi
}

echo "[INFO] service=${SERVICE_NAME}"
echo "[INFO] expected_interval=${EXPECTED_INTERVAL}"
echo

echo "[CHECK] unit exists"
if ! runctl status "${SERVICE_NAME}" --no-pager >/dev/null 2>&1; then
  echo "[WARN] service not found or inaccessible: ${SERVICE_NAME}"
  ((warnings++))
else
  echo "[OK] service is accessible"
fi
echo

echo "[CHECK] effective unit (systemctl cat)"
runctl cat "${SERVICE_NAME}" || true
echo

echo "[CHECK] core properties"
runctl show "${SERVICE_NAME}" \
  -p FragmentPath \
  -p DropInPaths \
  -p WorkingDirectory \
  -p ExecStart \
  -p EnvironmentFiles \
  -p Environment || true
echo

execstart_value="$(runctl show "${SERVICE_NAME}" -p ExecStart --value 2>/dev/null || true)"

echo "[CHECK] expected main entrypoint"
if echo "${execstart_value}" | grep -q "kis_trend_atr_trading.main_multiday"; then
  echo "[OK] ExecStart includes main_multiday"
else
  echo "[WARN] ExecStart does not include main_multiday"
  ((warnings++))
fi

echo "[CHECK] expected interval"
if echo "${execstart_value}" | grep -q -- "--interval ${EXPECTED_INTERVAL}"; then
  echo "[OK] ExecStart includes --interval ${EXPECTED_INTERVAL}"
else
  echo "[WARN] ExecStart does not include --interval ${EXPECTED_INTERVAL}"
  ((warnings++))
fi
echo

override_file="/etc/systemd/system/${SERVICE_NAME}.d/override.conf"
echo "[CHECK] override file sanity (${override_file})"
if (( USE_SUDO == 1 )); then
  content="$(sudo cat "${override_file}" 2>/dev/null || true)"
else
  content="$(cat "${override_file}" 2>/dev/null || true)"
fi
if [[ -z "${content}" ]]; then
  echo "[WARN] override.conf not found/readable"
  ((warnings++))
else
  if echo "${content}" | grep -q "^ExecStart=$"; then
    echo "[OK] contains ExecStart reset line"
  else
    echo "[WARN] missing ExecStart= reset line"
    ((warnings++))
  fi
fi
echo

if (( warnings == 0 )); then
  echo "[OK] no warnings detected."
  exit 0
fi

echo "[WARN] warnings detected: ${warnings}"
if (( STRICT == 1 )); then
  exit 1
fi
exit 0
