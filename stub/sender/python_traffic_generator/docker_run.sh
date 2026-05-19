#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
./docker_build.sh

# scenario="2-scenario-parallel"
scenario="3-scenario-nested"

docker run --rm -it \
  --network host \
  -e DEMO_TARGET_URL=http://localhost:18080/v1/pipeline \
  -e DEMO_SCENARIO_FILE="/app/scenarios/${scenario}.json" \
  -v "${PWD}/scenarios:/app/scenarios:ro" \
  -v "${PWD}/routes:/app/routes:ro" \
  traffic_generator

docker container prune -f
