#!/bin/bash
set -euo pipefail
img_name="runner"

./docker_build.sh

docker build --target "$img_name" -t "worker_rust-$img_name" .

docker image prune -f

docker_run_flags=(--rm)
if [ -t 0 ] && [ -t 1 ]; then
  docker_run_flags+=(-it)
else
  docker_run_flags+=(-i)
fi

docker run "${docker_run_flags[@]}" \
  --add-host=host.docker.internal:host-gateway \
  -e OTEL_DEMO_TRACE_EXPORT=otlp \
  -e OTEL_DEMO_LOG_EXPORT=otlp \
  -e OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://host.docker.internal:4318/v1/traces \
  -e OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://host.docker.internal:4318/v1/metrics \
  -e OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://host.docker.internal:4318/v1/logs \
  -e OTEL_ENVIRONMENT=local \
  -e OTEL_DEMO_RESOURCE_TAG=lang-rust \
  -e DEMO_HTTP_ADDR=0.0.0.0:8080 \
  -e DEMO_HTTP_PATH=/v1/pipeline \
  -e DEMO_WORKER_ID=rs \
  "worker_rust-$img_name"

docker container prune -f
