#!/bin/bash
set -euo pipefail
W="$(cd "$(dirname "$0")/.." && pwd)"
docker build -t python_worker_lib -f "${W}/python_worker/Dockerfile" "${W}"
docker build -f "${W}/python_gateway/Dockerfile" -t python_gateway \
  --build-arg PYTHON_WORKER_IMAGE=python_worker_lib \
  "${W}"

docker image prune -f
