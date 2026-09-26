#!/usr/bin/env bash
# Paper-trade the same order with every algorithm side by side, routed across Kraken and Coinbase
# public BTC/USD books. No keys, no real orders.
# Fees are illustrative entry-tier taker fees in bps. Set your own tier:
#   KRAKEN_FEE_BPS=26 COINBASE_FEE_BPS=40 bash scripts/demo_compare.sh ...
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/python"
# Always build: Ninja is a no-op when nothing changed, and a stale binary would hide engine fixes.
bash scripts/build_release.sh > /dev/null
mapfile -t VENUE_ARGS < <(python -m slipstream.cli venue-flags --venues kraken,coinbase \
  --fees "kraken=${KRAKEN_FEE_BPS:-40},coinbase=${COINBASE_FEE_BPS:-60}")
if [[ ${#VENUE_ARGS[@]} -eq 0 ]]; then
  echo "venue-flags produced no engine arguments; aborting" >&2
  exit 1
fi
build/release/slipstream_engine --listen 127.0.0.1:50051 \
  --max-order-notional 2000 --max-position 0.1 \
  "${VENUE_ARGS[@]}" &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT
python -m slipstream.cli compare --engine 127.0.0.1:50051 --venues kraken,coinbase "$@"
