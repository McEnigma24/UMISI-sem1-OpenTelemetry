#!/bin/bash
set -euo pipefail
img_name="builder"


docker build --target "$img_name" -t "worker_rust-$img_name" .

docker image prune -f

: "${CARGO_BUILD_JOBS:=4}"
export CARGO_BUILD_JOBS

# Domyślnie każdy `docker run` miał pusty ``/usr/local/cargo/registry`` → Cargo znów pobierał
# indeks i kraty z internetu. Katalog na hoście (wolumen) utrzymuje cache między buildami.
# Pierwszy build nadal wymaga sieci; kolejne często już nie (tylko ewentualna weryfikacja indeksu).
# Pełne offline: ``export CARGO_NET_OFFLINE=true`` (Cargo nie łączy się z siecią) — wymaga już
# wypełnionego cache / ``cargo vendor`` (patrz ``Docker_start.sh``).
: "${CARGO_CACHE_DIR:=$HOME/.cache/umisi-otel-rust-cargo}"
mkdir -p "${CARGO_CACHE_DIR}/registry" "${CARGO_CACHE_DIR}/git"

docker_run_flags=(--rm)
if [ -t 0 ] && [ -t 1 ]; then
  docker_run_flags+=(-it)
else
  docker_run_flags+=(-i)
fi

# Opcjonalnie: ``CARGO_NET_OFFLINE=true|false`` — Cargo **nie** akceptuje pustego stringa ani „1”.
cargo_net_args=()
case "${CARGO_NET_OFFLINE-}" in
  true|false) cargo_net_args=(-e "CARGO_NET_OFFLINE=${CARGO_NET_OFFLINE}") ;;
  1|yes|on|ON) cargo_net_args=(-e CARGO_NET_OFFLINE=true) ;;
  0|no|off|OFF) cargo_net_args=(-e CARGO_NET_OFFLINE=false) ;;
esac

docker run "${docker_run_flags[@]}" \
  -e CARGO_BUILD_JOBS \
  "${cargo_net_args[@]}" \
  -v "$(pwd):/workspace" \
  -v "${CARGO_CACHE_DIR}/registry:/usr/local/cargo/registry" \
  -v "${CARGO_CACHE_DIR}/git:/usr/local/cargo/git" \
  "worker_rust-$img_name"

docker container prune -f
