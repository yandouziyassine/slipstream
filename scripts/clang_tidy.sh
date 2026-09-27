#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

cmake -S engine -B build/tidy -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DSLIPSTREAM_SANITIZE=OFF
# The generated protobuf headers under build/tidy/gen must exist on disk before clang-tidy can
# parse engine/src files that include them; building slipstream_proto runs that codegen without
# building the rest of the tree.
cmake --build build/tidy --target slipstream_proto

status=0
for f in engine/src/*.cpp engine/tests/*.cpp; do
  echo "== clang-tidy: $f"
  clang-tidy -p build/tidy --extra-arg=-Wno-unknown-warning-option "$f" || status=1
done
exit "$status"
