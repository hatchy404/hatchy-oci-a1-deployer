#!/usr/bin/env bash
# One GitHub-hosted job. GitHub cron cannot fire every 90s (minimum 5 minutes),
# so this process retries every 90s until the run budget ends.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

INTERVAL="${ATTEMPT_INTERVAL_SECONDS:-90}"
BUDGET="${MAX_RUN_SECONDS:-240}"
MODE="${1:---once}"
PY="${PYTHON:-python3}"

start=$SECONDS
while true; do
  set +e
  "$PY" "$ROOT/watcher.py" "$MODE"
  rc=$?
  set -e
  case "$rc" in
    0)
      exit 0
      ;;
    10)
      ;;
    *)
      exit "$rc"
      ;;
  esac
  elapsed=$((SECONDS - start))
  if (( elapsed + INTERVAL >= BUDGET )); then
    echo "capacity wait continues on the next scheduled run"
    exit 0
  fi
  sleep "$INTERVAL"
done
