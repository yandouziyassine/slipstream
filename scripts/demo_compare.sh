#!/usr/bin/env bash
# Paper-trade the same order with every algorithm side by side, routed across Kraken and Coinbase
# public BTC/USD books. No keys, no real orders.
# Fees are illustrative entry-tier taker fees in bps. Set your own tier:
#   KRAKEN_FEE_BPS=26 COINBASE_FEE_BPS=40 bash scripts/demo_compare.sh ...
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/python"
build/engine/slipstream_engine --listen 127.0.0.1:50051 \
  --max-order-notional 2000 --max-position 0.1 \
  --venue "kraken:fee_bps=${KRAKEN_FEE_BPS:-40}" \
  --venue "coinbase:fee_bps=${COINBASE_FEE_BPS:-60}" &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT
python -m slipstream.cli compare --engine 127.0.0.1:50051 --venues kraken,coinbase "$@"
