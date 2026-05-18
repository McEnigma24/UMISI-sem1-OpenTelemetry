#!/bin/bash
img_name="builder"


docker build --target "$img_name" -t "client_cpp-$img_name" .

docker image prune -f



docker run --rm -it \
  -v "$(pwd):/workspace" \
  "client_cpp-$img_name"

docker container prune -f
