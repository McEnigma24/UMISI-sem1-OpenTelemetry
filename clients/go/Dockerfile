# Jak clients/rust: builder z volumenem (./docker_build.sh), runner tylko COPY binarki z ./docker-target.
#
#   cd clients/go && ./docker_build.sh
#
FROM debian:bookworm-slim AS runtime-base
ENV DEBIAN_FRONTEND=noninteractive \
    TERM=xterm-256color
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

FROM golang:1.23-bookworm AS dev-env
ENV GOTOOLCHAIN=local
WORKDIR /workspace

FROM dev-env AS builder
CMD ["/bin/sh", "-c", "sleep 1 && exec /usr/bin/bash /workspace/Docker_start.sh"]

FROM runtime-base AS runner
WORKDIR /app
COPY docker-target/client_go /app/client_go
CMD ["/bin/sh", "-c", "sleep 2 && exec /app/client_go"]
