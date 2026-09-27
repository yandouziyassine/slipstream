#!/usr/bin/env bash
# Builds the release engine, then runs bench/pipeline_bench.py against it.
# Fails if p50 client latency exceeds SLIPSTREAM_BENCH_TOLERANCE x bench/baseline.json (default 2x).
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/build_release.sh > /dev/null
export PYTHONPATH="$PWD/python"
python bench/pipeline_bench.py \
  --baseline bench/baseline.json \
  --tolerance "${SLIPSTREAM_BENCH_TOLERANCE:-2.0}"
