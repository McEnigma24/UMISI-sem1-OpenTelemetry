#!/bin/bash
set -euo pipefail
# Uruchamiaj z katalogu UMISI-sem1-OpenTelemetry (tak jak compose_run).

clear
echo "compose_build: Rust (Dockerfile builder + volumen /workspace)…"
( cd clients/rust && ./docker_build.sh )

clear
echo "compose_build: C# (Dockerfile builder + volumen /workspace)…"
( cd clients/csharp && ./docker_build.sh )

clear
echo "compose_build: Go (Dockerfile builder + volumen /workspace)…"
( cd clients/go && ./docker_build.sh )

clear
echo "compose_build: Java (Dockerfile: Maven shade)…"
( cd clients/java && ./docker_build.sh )

clear
# Node.js (opcjonalnie): ``docker compose --profile nodejs build client-nodejs`` — nie w domyślnym stacku.
# echo "compose_build: Node.js …"
# ( cd clients/nodejs && ./docker_build.sh )

clear
docker compose build --parallel

docker image prune -f
