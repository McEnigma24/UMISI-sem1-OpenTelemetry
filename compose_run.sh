#!/bin/bash
./compose_build.sh || exit 1

# docker compose up

# docker compose build --no-cache gateway-python
# docker compose build --no-cache worker-rust
# docker compose build --no-cache worker-csharp

docker compose up -d --force-recreate || exit 1
docker compose logs -f gateway-python worker-rust worker-csharp worker-go worker-java
# docker compose logs -f otel 2>&1 # 'forward' - 'received' - 'Body:'

docker container prune -f
docker compose down



# docker compose run --rm
