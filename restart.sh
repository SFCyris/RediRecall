#!/usr/bin/env bash
# RediRecall — restart: stop the app + dedicated Redis, then start them again.
# Any arguments (e.g. a port) are forwarded to start.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"${SCRIPT_DIR}/stop.sh" || true
sleep 1
exec "${SCRIPT_DIR}/start.sh" "$@"
