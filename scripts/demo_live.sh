#!/usr/bin/env bash
# Paper-trade one TWAP order against Kraken's live public BTC/USD book. No keys, no real orders.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/python"
build/engine/slipstream_engine --listen 127.0.0.1:50051 \
  --max-order-notional 2000 --max-position 0.05 &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT
python -m slipstream.cli live --engine 127.0.0.1:50051 "$@"
