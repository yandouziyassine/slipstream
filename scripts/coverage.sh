#!/usr/bin/env bash
# Reports Python and C++ test coverage. Report only: there is no coverage threshold gate here,
# on purpose (a gate pushes people to write tests that move the number instead of tests that
# catch bugs). The script exits nonzero only if the C++ or Python tests themselves fail.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "== generate protobuf stubs"
bash scripts/gen_proto.sh

echo "== configure C++ coverage build (no sanitizers)"
cmake -S engine -B build/coverage -G Ninja \
  -DCMAKE_BUILD_TYPE=Debug \
  -DSLIPSTREAM_SANITIZE=OFF \
  -DSLIPSTREAM_COVERAGE=ON
cmake --build build/coverage

mkdir -p build/coverage-report/cpp build/coverage-report/python

echo "== C++ tests (coverage build)"
set +e
ctest --test-dir build/coverage --output-on-failure
cpp_status=$?
set -e

echo "== C++ coverage report"
# GCC's gcov occasionally emits a negative branch hit count on optimized code (a known gcov bug,
# https://gcc.gnu.org/bugzilla/show_bug.cgi?id=68080); treat it as a warning rather than aborting.
gcovr --root . \
  --filter 'engine/src/' \
  --exclude 'engine/tests/' \
  --exclude '.*/gen/.*' \
  --gcov-ignore-parse-errors negative_hits.warn_once_per_file \
  --sort uncovered-percent --sort-reverse \
  --print-summary \
  --txt build/coverage-report/cpp/summary.txt \
  --html-details build/coverage-report/cpp/index.html \
  build/coverage

echo "== build the engine binary the Python tests run against"
bash scripts/build_engine.sh

echo "== Python tests with coverage"
set +e
(
  cd python
  python3 -m pytest --cov=slipstream --cov-report=term-missing \
    --cov-report="html:${ROOT}/build/coverage-report/python" -q
) | tee build/coverage-report/python/summary.txt
python_status=$?
set -e

if [[ "$cpp_status" -ne 0 || "$python_status" -ne 0 ]]; then
  echo "scripts/coverage.sh: tests failed (C++ ctest exit=$cpp_status, pytest exit=$python_status)" >&2
  exit 1
fi

echo "coverage OK (report only, no threshold gate)"
