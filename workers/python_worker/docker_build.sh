#!/bin/bash
set -euo pipefail
# Buduje oba obrazy Python z korzenia repozytorium (osobne Dockerfile, bez wspólnej bazy).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"
docker build -f python_gateway/Dockerfile -t gateway_python .
docker build -f workers/python_worker/Dockerfile -t worker_python .
docker image prune -f
