#!/bin/bash

# docker build --no-cache -t traffic_generator .
docker build -t traffic_generator .

docker image prune -f
