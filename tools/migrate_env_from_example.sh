#!/usr/bin/env bash
set -euo pipefail

# Append missing keys from .env.example into .env without overwriting existing values.
#
# Usage:
#   tools/migrate_env_from_example.sh [ENV_FILE] [EXAMPLE_FILE]
# Example:
#   tools/migrate_env_from_example.sh \
#     /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading/.env \
#     /home/deploy/KIS-API-Trend-ATR/kis_trend_atr_trading/.env.example

ENV_FILE="${1:-kis_trend_atr_trading/.env}"
EXAMPLE_FILE="${2:-kis_trend_atr_trading/.env.example}"

if [[ ! -f "$EXAMPLE_FILE" ]]; then
  echo "[ERROR] example file not found: $EXAMPLE_FILE" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE_FILE" "$ENV_FILE"
  echo "[OK] env file did not exist. created from example: $ENV_FILE"
  exit 0
fi

BACKUP_FILE="${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
cp "$ENV_FILE" "$BACKUP_FILE"
echo "[OK] backup created: $BACKUP_FILE"

TMP_ADD="$(mktemp)"
ADDED_COUNT=0

has_key() {
  local key="$1"
  local file="$2"
  if command -v rg >/dev/null 2>&1; then
    rg -q "^${key}=" "$file"
  else
    grep -qE "^${key}=" "$file"
  fi
}

while IFS= read -r line; do
  [[ "$line" =~ ^[A-Z][A-Z0-9_]*= ]] || continue
  key="${line%%=*}"
  if ! has_key "$key" "$ENV_FILE"; then
    echo "$line" >> "$TMP_ADD"
    ADDED_COUNT=$((ADDED_COUNT + 1))
  fi
done < "$EXAMPLE_FILE"

if (( ADDED_COUNT == 0 )); then
  rm -f "$TMP_ADD"
  echo "[OK] no missing keys. env is already up to date."
  exit 0
fi

{
  echo
  echo "# Added by tools/migrate_env_from_example.sh on $(date '+%Y-%m-%d %H:%M:%S %Z')"
  cat "$TMP_ADD"
} >> "$ENV_FILE"

echo "[OK] added ${ADDED_COUNT} missing keys to: $ENV_FILE"
echo "[INFO] added keys:"
sed -E 's/=.*$//' "$TMP_ADD"

rm -f "$TMP_ADD"
