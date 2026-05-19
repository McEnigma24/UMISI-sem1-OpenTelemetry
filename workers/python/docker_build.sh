#!/bin/bash
docker build -t worker_python .

docker image prune -f
