#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cmake -S engine -B build/release -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DSLIPSTREAM_SANITIZE=OFF
cmake --build build/release
