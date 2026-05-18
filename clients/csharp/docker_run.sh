#!/bin/bash
set -euo pipefail
./docker_build.sh

docker build -t client_csharp --target runner .

docker_run_flags=(--rm)
if [ -t 0 ] && [ -t 1 ]; then
  docker_run_flags+=(-it)
else
  docker_run_flags+=(-i)
fi

docker run "${docker_run_flags[@]}" \
  --add-host=host.docker.internal:host-gateway \
  -e OTEL_DEMO_TRACE_EXPORT=otlp \
  -e OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://host.docker.internal:4318/v1/traces \
  -e OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://host.docker.internal:4318/v1/metrics \
  -e OTEL_ENVIRONMENT=local \
  -e OTEL_DEMO_RESOURCE_TAG=lang-csharp \
  client_csharp

docker container prune -f
