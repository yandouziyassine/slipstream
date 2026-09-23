#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cmake -S engine -B build/engine -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DSLIPSTREAM_SANITIZE="${SLIPSTREAM_SANITIZE:-ON}"
cmake --build build/engine
