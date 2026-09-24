#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== generate protobuf stubs"
bash scripts/gen_proto.sh

echo "== build engine (ASan + UBSan)"
bash scripts/build_engine.sh

echo "== C++ tests"
ctest --test-dir build/engine --output-on-failure

echo "== Python lint + types"
(cd python && ruff check . && ruff format --check . && mypy slipstream)

echo "== Python tests (unit + integration)"
(cd python && pytest -q)

echo "== dependency audit"
pip-audit

echo "CI OK"
