#!/bin/bash
img_name="dev-env"


docker build --target "$img_name" -t "worker_cpp-$img_name" .

docker image prune -f



docker run --rm -it \
  -v "$(pwd):/workspace" \
  "worker_cpp-$img_name"

docker container prune -f
