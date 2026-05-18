#!/bin/bash
set -euo pipefail
img_name="builder"


docker build --target "$img_name" -t "client_rust-$img_name" .

docker image prune -f

: "${CARGO_BUILD_JOBS:=4}"
export CARGO_BUILD_JOBS

docker_run_flags=(--rm)
if [ -t 0 ] && [ -t 1 ]; then
  docker_run_flags+=(-it)
else
  docker_run_flags+=(-i)
fi

docker run "${docker_run_flags[@]}" \
  -e CARGO_BUILD_JOBS \
  -v "$(pwd):/workspace" \
  "client_rust-$img_name"

docker container prune -f
