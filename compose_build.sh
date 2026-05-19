#!/bin/bash
set -euo pipefail
# Zawsze katalog z tym skryptem i ``docker-compose.yml`` (unikamy złego build context dla gateway/worker Python).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

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


echo "compose_build: Docker Compose (m.in. gateway_python, worker_python, otel, … — równolegle)…"
# Obrazy Python: osobne projekty — ``python_gateway/Dockerfile`` → ``gateway_python``,
# ``workers/python_worker/Dockerfile`` → ``worker_python`` (oba context: katalog repo).
docker compose build --parallel

docker image prune -f
