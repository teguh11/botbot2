#!/usr/bin/env bash
# Load secrets from .env, then run freqtrade in the project venv.
#
# Usage:
#   ./run.sh trade      --strategy SupertrendSD
#   ./run.sh backtesting --strategy SupertrendSD --timerange 20260401-
#   ./run.sh webserver
#
# --userdir and --config are appended automatically.
set -euo pipefail
cd "$(dirname "$0")"

set -a
[ -f .env ] && . ./.env
set +a

exec .venv/bin/freqtrade "$@" \
    --userdir user_data \
    --config user_data/config.json
