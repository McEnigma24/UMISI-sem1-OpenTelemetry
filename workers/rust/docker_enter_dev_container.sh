#!/bin/bash
set -euo pipefail
img_name="dev-env"


docker build --target "$img_name" -t "worker_rust-$img_name" .

docker image prune -f

docker_run_flags=(--rm)
if [ -t 0 ] && [ -t 1 ]; then
  docker_run_flags+=(-it)
else
  docker_run_flags+=(-i)
fi

docker run "${docker_run_flags[@]}" \
  -v "$(pwd):/workspace" \
  "worker_rust-$img_name"

docker container prune -f
