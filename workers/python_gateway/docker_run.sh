#!/bin/bash
set -euo pipefail
./docker_build.sh

docker run --rm -it \
  --add-host=host.docker.internal:host-gateway \
  -e OTEL_DEMO_TRACE_EXPORT=otlp \
  -e OTEL_DEMO_LOG_EXPORT=otlp \
  -e OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://host.docker.internal:4318/v1/traces \
  -e OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://host.docker.internal:4318/v1/metrics \
  -e OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://host.docker.internal:4318/v1/logs \
  -e OTEL_ENVIRONMENT=local \
  -e OTEL_DEMO_RESOURCE_TAG=lang-python \
  python_gateway

docker container prune -f
