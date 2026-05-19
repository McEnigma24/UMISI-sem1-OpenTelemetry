#!/usr/bin/env bash
# Wywoływany z compose_build: ``( cd clients/java && ./docker_build.sh )``.
# Buduje obraz ``client_java`` (Dockerfile: mvn package + JRE).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
exec docker compose build client-java
