#!/usr/bin/env bash
# Jak clients/go: wywoływany z compose_build przez ``( cd clients/nodejs && ./docker_build.sh )``.
# Buduje obraz ``client-nodejs`` (Dockerfile: npm install + index.mjs z kontekstu ./clients/nodejs).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
exec docker compose build client-nodejs
