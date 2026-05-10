#!/usr/bin/env bash
# Wywoływany z kontenera builder przy volumenie ./clients/rust → /workspace
set -euo pipefail
cd /workspace
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/workspace/docker-target}"
mkdir -p "${CARGO_TARGET_DIR}"
exec cargo build --release --bin client_rust
