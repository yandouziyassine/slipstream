#!/usr/bin/env bash
# Paper-trade the same order with every algorithm side by side on Kraken's live public BTC/USD book.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/python"
build/engine/slipstream_engine --listen 127.0.0.1:50051 \
  --max-order-notional 2000 --max-position 0.1 &
ENGINE_PID=$!
trap 'kill "$ENGINE_PID" 2>/dev/null || true' EXIT
python -m slipstream.cli compare --engine 127.0.0.1:50051 "$@"
