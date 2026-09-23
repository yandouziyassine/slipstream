#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
"${PYTHON:-python}" -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out=python \
  --pyi_out=python \
  --grpc_python_out=python \
  proto/slipstream/v1/execution.proto
