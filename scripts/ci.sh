#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== generate protobuf stubs"
bash scripts/gen_proto.sh

echo "== build engine (ASan + UBSan)"
bash scripts/build_engine.sh

echo "== C++ tests"
ctest --test-dir build/engine --output-on-failure

echo "== C++ loop, queue and stream tests under ThreadSanitizer"
# GCC 13's TSan cannot map its shadow memory under high-entropy ASLR, so disable ASLR where the
# sandbox allows it. gRPC, protobuf and abseil are system libraries built without TSan, so TSan
# cannot see their internal synchronization: ignore the memory accesses made inside them.
no_aslr() {
  if setarch "$(uname -m)" -R true 2>/dev/null; then setarch "$(uname -m)" -R "$@"; else "$@"; fi
}
export TSAN_OPTIONS="ignore_noninstrumented_modules=1"
no_aslr cmake -S engine -B build/tsan -G Ninja -DSLIPSTREAM_TSAN=ON -DSLIPSTREAM_SANITIZE=OFF \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo
no_aslr cmake --build build/tsan
# The wake-latency test asserts speed, not thread safety; TSan slows it past its threshold.
no_aslr ctest --test-dir build/tsan -R "EngineLoop|Stream|BoundedQueue" \
  -E "CommandWakesAnIdleLoopImmediately" --output-on-failure
unset TSAN_OPTIONS

echo "== Python lint + types"
(cd python && ruff check . && ruff format --check . && mypy slipstream)

echo "== Python tests (unit + integration)"
(cd python && pytest -q)

echo "== dependency audit"
pip-audit

echo "CI OK"
