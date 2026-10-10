#!/usr/bin/env bash
# Run one hour of paper-trading evidence collection: build the release engine, fetch the venue
# flags, then run the collector, which starts a fresh engine for each order size on a loopback
# port picked by the OS and always stops it after that size's run. Paper trading only; no real
# orders, no keys. Meant to be run hourly by Windows Task Scheduler through WSL
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

# One engine per size run, started and stopped by the collector itself (see
# slipstream/collect.py): orders a failed size run leaves working can never trade into, or count
# against the position limit of, the next size's run. The engine logs go to
# $DATA/logs/engine-<stamp>-<size>.log. A SIGTERM to the collector stops the engine of the run in
# progress before it exits.
python -m slipstream.collect run --engine-binary "$BINARY" "${VENUE_ARGS[@]}"

# Building and publishing the research page is best-effort: a failure here must never mark the
# hourly collection itself as failed, since the evidence is already safely in the database.
python -m slipstream.site build || echo "site build failed" >&2
python -m slipstream.site publish || echo "site publish failed" >&2
