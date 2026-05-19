#!/usr/bin/env bash
set -euo pipefail
img_name="builder"
# Jedna wolumen-cache na maszynę — kolejne buildy Go bez ponownego ściągania modułów z sieci.
gomod_vol="${GOMOD_CACHE_VOLUME:-umisi_worker_go_gomodcache}"
docker volume inspect "$gomod_vol" >/dev/null 2>&1 || docker volume create "$gomod_vol" >/dev/null

docker build --target "$img_name" -t "worker_go-$img_name" .

docker image prune -f

docker_run_flags=(--rm)
if [ -t 0 ] && [ -t 1 ]; then
  docker_run_flags+=(-it)
else
  docker_run_flags+=(-i)
fi

docker run "${docker_run_flags[@]}" \
  -v "$(pwd):/workspace" \
  -v "${gomod_vol}:/go/pkg/mod" \
  "worker_go-$img_name"

docker container prune -f
