#!/usr/bin/env bash
# Jak workers/go: wywoływany z compose_build przez ``( cd workers/nodejs && ./docker_build.sh )``.
# Buduje obraz ``worker-nodejs`` (Dockerfile: npm install + index.mjs z kontekstu ./workers/nodejs).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
exec docker compose build worker-nodejs
