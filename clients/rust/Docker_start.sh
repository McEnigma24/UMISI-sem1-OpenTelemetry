#!/usr/bin/env bash
# Wywoływany z kontenera builder przy volumenie ./clients/rust → /workspace
# CARGO_BUILD_JOBS — domyślnie 2, żeby ograniczyć RAM (bez limitu cargo bierze ~liczbę rdzeni
# i przy dużym projekcie WSL potrafi dostać OOM → „ginie” terminal / sesja).
set -euo pipefail
cd /workspace
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/workspace/docker-target}"
: "${CARGO_BUILD_JOBS:=2}"
export CARGO_BUILD_JOBS
mkdir -p "${CARGO_TARGET_DIR}"
exec cargo build --release -j "$CARGO_BUILD_JOBS" --bin client_rust
