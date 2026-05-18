#!/bin/bash
set -euo pipefail
# Uruchamiaj z katalogu UMISI-sem1-OpenTelemetry (tak jak compose_run).

echo "compose_build: Rust (Dockerfile builder + volumen /workspace)…"
( cd clients/rust && ./docker_build.sh )

echo "compose_build: C# (Dockerfile builder + volumen /workspace)…"
( cd clients/csharp && ./docker_build.sh )

clear
docker compose build --parallel

docker image prune -f
