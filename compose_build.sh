#!/bin/bash
set -euo pipefail
# Uruchamiaj z katalogu UMISI-sem1-OpenTelemetry (tak jak compose_run).

clear
echo "compose_build: Rust (Dockerfile builder + volumen /workspace)…"
( cd workers/rust && ./docker_build.sh )

clear
echo "compose_build: C# (Dockerfile builder + volumen /workspace)…"
( cd workers/csharp && ./docker_build.sh )

clear
echo "compose_build: Go (Dockerfile builder + volumen /workspace)…"
( cd workers/go && ./docker_build.sh )

clear
echo "compose_build: Java (Dockerfile: Maven shade)…"
( cd workers/java && ./docker_build.sh )

clear
# Node.js (opcjonalnie): ``docker compose --profile nodejs build worker-nodejs`` — nie w domyślnym stacku.
# echo "compose_build: Node.js …"
# ( cd workers/nodejs && ./docker_build.sh )

clear
docker compose build --parallel

docker image prune -f
