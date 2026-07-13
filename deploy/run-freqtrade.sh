#!/usr/bin/env bash
# Dijalankan oleh systemd (freqtrade.service).
# Baca file mode → jalankan strategy yang sesuai.
#   nfi     → NFIX7Verbose  (sabar, proven)      [default]
#   active  → SupertrendSD  (agresif, sering entry)
set -euo pipefail
cd "$(dirname "$0")/.."   # → repo root

MODE_FILE="user_data/active_mode"
MODE="nfi"
[ -f "$MODE_FILE" ] && MODE="$(tr -d '[:space:]' < "$MODE_FILE")"

if [ "$MODE" = "active" ]; then
  CONFIG="user_data/config_active.json"
  STRATEGY="SupertrendSD"
else
  CONFIG="user_data/config_nfi_futures.json"
  STRATEGY="NFIX7Verbose"
fi

echo "▶ Starting freqtrade | mode=$MODE | strategy=$STRATEGY | config=$CONFIG"
exec .venv/bin/freqtrade trade \
  --userdir user_data \
  --config "$CONFIG" \
  --strategy "$STRATEGY"
