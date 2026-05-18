#!/bin/bash
img_name="builder"


docker build --target "$img_name" -t "client_rust-$img_name" .

docker image prune -f

: "${CARGO_BUILD_JOBS:=4}"
export CARGO_BUILD_JOBS

docker run --rm -it \
  -e CARGO_BUILD_JOBS \
  -v "$(pwd):/workspace" \
  "client_rust-$img_name"

docker container prune -f
