#!/bin/bash
set -euo pipefail
./docker_build.sh

scenario="2-scenario-parallel.json"

docker run --rm -it \
  --network host \
  -e DEMO_TARGET_URL=http://localhost:18080/v1/pipeline \
  -e DEMO_SCENARIO_FILE="/app/scenarios/${scenario}.json" \
  -v "$TG_ROOT/scenarios:/app/scenarios:ro" \
  -v "$TG_ROOT/routes:/app/routes:ro" \
  traffic_generator

docker container prune -f
