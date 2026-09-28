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

# Skip the build entirely when the release binary is already newer than every engine/proto
# source file (an hourly run rebuilding an unchanged engine wastes CPU and disk for nothing).
# When a build is actually needed, run it niced and with the lowest I/O class available so it
# never competes with the live engine or the collector below, which stay at normal priority so
# the latency we measure each hour stays honest.
BINARY="build/release/slipstream_engine"
NEEDS_BUILD=1
if [[ -f "$BINARY" ]] && [[ -z "$(find engine proto -type f -newer "$BINARY" -print -quit)" ]]; then
  NEEDS_BUILD=0
fi
if [[ "$NEEDS_BUILD" -eq 1 ]]; then
  if command -v ionice >/dev/null 2>&1; then
    nice -n 19 ionice -c3 bash scripts/build_release.sh
  else
    nice -n 19 bash scripts/build_release.sh
  fi
fi

# Captured via `$(...)`, not `< <(...)`: a process substitution's exit status is invisible to
# `set -e`, so a failing venue-flags call (e.g. a network error) would otherwise go unnoticed.
VENUE_OUTPUT="$(python -m slipstream.cli venue-flags --venues kraken,coinbase \
  --fees "${SLIPSTREAM_VENUE_FEES:-kraken=40,coinbase=60}")"
mapfile -t VENUE_ARGS <<<"$VENUE_OUTPUT"
if [[ ${#VENUE_ARGS[@]} -eq 0 ]]; then
  echo "venue-flags produced no engine arguments; aborting" >&2
  exit 1
fi

ENGINE_LOG="$DATA/logs/engine-$STAMP.log"
build/release/slipstream_engine --listen 127.0.0.1:0 --clock live \
  --max-order-notional 50000 --max-position 2 \
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

# Building and publishing the research page is best-effort: a failure here must never mark the
# hourly collection itself as failed, since the evidence is already safely in the database.
python -m slipstream.site build || echo "site build failed" >&2
python -m slipstream.site publish || echo "site publish failed" >&2
