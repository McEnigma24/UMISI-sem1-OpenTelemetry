#!/usr/bin/env bash
# Wywoływany z kontenera builder przy volumenie ./workers/rust → /workspace
# CARGO_BUILD_JOBS — domyślnie 2, żeby ograniczyć RAM (bez limitu cargo bierze ~liczbę rdzeni
# i przy dużym projekcie WSL potrafi dostać OOM → „ginie” terminal / sesja).
set -euo pipefail
cd /workspace
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-/workspace/docker-target}"
: "${CARGO_BUILD_JOBS:=2}"
export CARGO_BUILD_JOBS
mkdir -p "${CARGO_TARGET_DIR}"
# ``CARGO_NET_OFFLINE`` w Cargo musi być dokładnie ``true`` albo ``false`` (pusty string = błąd).
# Mapujemy 1/0; inne / puste — usuwamy z env przed ``cargo``.
build_args=(--release -j "$CARGO_BUILD_JOBS" --bin worker_rust)
_n="${CARGO_NET_OFFLINE-}"
case "$_n" in
  true|1|yes|on|ON)
    export CARGO_NET_OFFLINE=true
    build_args+=(--locked)
    ;;
  false|0|no|off|OFF)
    export CARGO_NET_OFFLINE=false
    ;;
  *)
    unset CARGO_NET_OFFLINE
    ;;
esac
exec cargo build "${build_args[@]}"
