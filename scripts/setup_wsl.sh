#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# One-time, idempotent setup for a fresh WSL2 Ubuntu 24.04 install. Safe to
# re-run: apt-get install and pip install are both no-ops on an unchanged
# system, and the venv is only created if it doesn't already exist.

# This package list must stay identical to the "Install C++ toolchain" step
# in .github/workflows/ci.yml, so a WSL setup matches what CI runs. If you
# change one, change the other.
PACKAGES=(
  cmake ninja-build pkg-config
  libgrpc++-dev libprotobuf-dev protobuf-compiler protobuf-compiler-grpc
  libgtest-dev python3-venv
)

echo "== Installing apt packages: ${PACKAGES[*]}"
sudo apt-get update
sudo apt-get install -y --no-install-recommends "${PACKAGES[@]}"

VENV_DIR="$HOME/.venvs/slipstream"
if [ ! -d "$VENV_DIR" ]; then
  echo "== Creating venv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
else
  echo "== Venv already exists at $VENV_DIR, reusing it"
fi

echo "== Installing Python dependencies (hash-verified) into $VENV_DIR"
"$VENV_DIR/bin/pip" install --require-hashes -r python/requirements-dev.txt

echo "== setup_wsl.sh done."
echo "Add the venv to PATH in your shell profile, e.g.:"
echo '  export PATH="$HOME/.venvs/slipstream/bin:$PATH"'
