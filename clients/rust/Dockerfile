# Jak clients/cpp:
#   • runtime-base — minimalny runtime
#   • dev-env — toolchain Rust
#   • builder — ZERO COPY źródeł; montujesz repo: -v "$(pwd):/workspace", potem ./Docker_start.sh
#   • runner — tylko binarka z hosta (po ./docker_build): COPY ./docker-target/release/client_rust
#
# Workflow:
#   cd clients/rust && ./docker_build    # kompilacja (uruchom ./ a nie „. docker_build”)
#   cd clients/rust && ./docker_run      # obraz runner + uruchomienie (albo: docker compose build)

# ==========================================
# 1. RUNTIME BASE
# ==========================================
FROM debian:bookworm-slim AS runtime-base
ENV DEBIAN_FRONTEND=noninteractive \
    TERM=xterm-256color
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# ==========================================
# 2. DEV / toolchain (bez kopiowania projektu)
# ==========================================
FROM rust:1-bookworm AS dev-env
WORKDIR /workspace
ENV CARGO_TARGET_DIR=/workspace/docker-target

# ==========================================
# 3. BUILDER — źródła wyłącznie z volumenu (patrz docker_build)
# ==========================================
FROM dev-env AS builder
# Skrypt jest tylko na volumenie /workspace — wywołanie przez bash (bez chmod +x na bind-mount).
CMD ["/bin/sh", "-c", "sleep 2 && exec /usr/bin/bash /workspace/Docker_start.sh"]

# ==========================================
# 4. RUNNER — artefakt z ./docker-target na hoście (jak cpp: ./build/cpp_client.exe)
# ==========================================
FROM runtime-base AS runner
WORKDIR /app
COPY ./docker-target/release/client_rust /app/client_rust
CMD ["/bin/sh", "-c", "sleep 2 && exec /app/client_rust"]
