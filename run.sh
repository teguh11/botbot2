#!/usr/bin/env bash
# Load secrets from .env, then run freqtrade in the project venv.
# Pass --config yourself (configs live in user_data/):
#
#   NFI (spot, active):
#     ./run.sh backtesting --config user_data/config_nfi.json --strategy NostalgiaForInfinityX7 --timerange 20260101-
#     ./run.sh trade       --config user_data/config_nfi.json --strategy NostalgiaForInfinityX7
#     ./run.sh webserver   --config user_data/config_nfi.json
#
#   SupertrendSD (retired, futures):
#     ./run.sh backtesting --config user_data/config.json --strategy SupertrendSD --timerange 20260101-
#
# --userdir is appended automatically.
set -euo pipefail
cd "$(dirname "$0")"

set -a
[ -f .env ] && . ./.env
set +a

exec .venv/bin/freqtrade "$@" --userdir user_data
