#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== generate protobuf stubs"
bash scripts/gen_proto.sh

echo "== build engine (ASan + UBSan)"
bash scripts/build_engine.sh

echo "== Hypothesis property tests (nightly profile: random seed, max_examples=2000)"
(cd python && SLIPSTREAM_HYPOTHESIS_PROFILE=nightly pytest -q tests/test_properties.py)

# These depend on real concurrency or wall-clock timing rather than pure logic: a live engine
# subprocess reached over gRPC, an asyncio event loop racing background tasks, or a websocket
# server standing in for an exchange feed. A fixed-seed unit run can pass by luck; repeating them
# gives timing races a real chance to show up instead of waiting for one to surface on its own.
TIMING_SENSITIVE_TESTS=(
  tests/test_cli.py
  tests/test_collect.py
  tests/test_engine_stream.py
  tests/test_feed_process.py
  tests/test_integration_replay.py
  tests/test_integration_routing.py
  tests/test_integration_schedules.py
  tests/test_live_session.py
  tests/test_recorder.py
  tests/test_session.py
)

REPEATS="${SLIPSTREAM_NIGHTLY_REPEATS:-20}"

echo "== repeating timing-sensitive tests ${REPEATS} times"
(
  cd python
  for ((i = 1; i <= REPEATS; i++)); do
    echo "-- iteration ${i}/${REPEATS}"
    if ! pytest -x -q "${TIMING_SENSITIVE_TESTS[@]}"; then
      echo "nightly: timing-sensitive tests failed on iteration ${i}/${REPEATS}"
      exit 1
    fi
  done
)

echo "nightly OK"
