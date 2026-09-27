#!/usr/bin/env bash
# Run one hour of paper-trading evidence collection: build the release engine, start it on a
# loopback port picked by the OS, run the collector against it, then stop it. Paper trading only;
# no real orders, no keys. Meant to be run hourly by Windows Task Scheduler through WSL
# (scripts/install_task.ps1), but it is also safe to run by hand.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/python"
# Task Scheduler runs this non-interactively, so ~/.bashrc is never sourced: put the project
# venv on PATH ourselves when it exists, rather than relying on an already-activated shell.
if [[ -d "$HOME/.venvs/slipstream/bin" ]]; then
  export PATH="$HOME/.venvs/slipstream/bin:$PATH"
fi

DATA="${SLIPSTREAM_DATA_DIR:-$HOME/slipstream-data}"
mkdir -p "$DATA/logs"
STAMP="$(date -u +%Y-%m-%d-%H%M%S)"
exec >>"$DATA/logs/collect_hourly-$(date -u +%Y-%m-%d).log" 2>&1
echo "=== collect_hourly $STAMP UTC ==="

bash scripts/build_release.sh

mapfile -t VENUE_ARGS < <(python -m slipstream.cli venue-flags --venues kraken,coinbase \
  --fees "${SLIPSTREAM_VENUE_FEES:-kraken=40,coinbase=60}")
if [[ ${#VENUE_ARGS[@]} -eq 0 ]]; then
  echo "venue-flags produced no engine arguments; aborting" >&2
  exit 1
fi

ENGINE_LOG="$DATA/logs/engine-$STAMP.log"
build/release/slipstream_engine --listen 127.0.0.1:0 --clock live \
  --max-order-notional 50000 --max-position 1 \
  "${VENUE_ARGS[@]}" >"$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true; wait "$ENGINE_PID" 2>/dev/null || true' EXIT

PORT=""
for _ in $(seq 1 100); do
  if [[ -s "$ENGINE_LOG" ]]; then
    PORT="$(grep -oP 'listening on port \K[0-9]+' "$ENGINE_LOG" | head -1 || true)"
    [[ -n "$PORT" ]] && break
  fi
  if ! kill -0 "$ENGINE_PID" 2>/dev/null; then
    echo "engine exited before printing its port" >&2
    cat "$ENGINE_LOG" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ -z "$PORT" ]]; then
  echo "engine did not print its port in time" >&2
  exit 1
fi
echo "engine listening on 127.0.0.1:$PORT"

python -m slipstream.collect run --engine "127.0.0.1:$PORT"
