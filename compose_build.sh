#!/bin/bash
# Uruchamiaj z katalogu UMISI-sem1-OpenTelemetry (tak jak compose_run).

RUST_BIN="clients/rust/docker-target/release/client_rust"
if [ ! -f "$RUST_BIN" ]; then
  echo "compose_build: brak $RUST_BIN — najpierw: cd clients/rust && ./docker_build.sh"
  exit 1
fi

docker compose build --parallel

docker image prune -f
