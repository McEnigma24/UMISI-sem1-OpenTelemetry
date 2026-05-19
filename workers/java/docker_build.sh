#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Wywoływany z compose_build: ``( cd workers/java && ./docker_build.sh )``.
# Buduje obraz ``worker_java`` (Dockerfile: mvn package + JRE).
if ! command -v docker >/dev/null 2>&1; then
  echo "docker: not found" >&2
  exit 1
fi
exec docker compose build worker-java
