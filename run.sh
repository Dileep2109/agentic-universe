#!/usr/bin/env bash
# Start the controller and run until the simulator scenario finishes.
# Usage: ./run.sh <SIM_URL>
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
STATE_DIR="${CONTROLLER_STATE_DIR:-$ROOT/state}"
exec python -m controller --url "$SIM_URL" --state-dir "$STATE_DIR"
