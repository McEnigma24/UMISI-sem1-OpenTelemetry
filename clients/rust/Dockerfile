# Jak clients/cpp:
#   • runtime-base — minimalny runtime
#   • dev-env — toolchain Rust (workflow z volumenem: ./docker_build.sh)
#   • builder — ZERO COPY źródeł; montujesz repo: -v "$(pwd):/workspace", potem ./Docker_start.sh
#   • compile — pełna kompilacja w obrazie (docker compose build — zawsze świeży kod, m.in. propagator W3C)
#   • runner — binarka z etapu compile (compose) albo z ./docker-target (stary skrypt na hoście)
#
# Workflow lokalny (volumen):
#   cd clients/rust && ./docker_build.sh
#
# Workflow compose (bez hostowego cargo):
#   docker compose build client-rust

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
# 3. BUILDER — źródła wyłącznie z volumenu (patrz docker_build.sh)
# ==========================================
FROM dev-env AS builder
# Skrypt jest tylko na volumenie /workspace — wywołanie przez bash (bez chmod +x na bind-mount).
CMD ["/bin/sh", "-c", "sleep 2 && exec /usr/bin/bash /workspace/Docker_start.sh"]

# ==========================================
# 4. COMPILE — źródła z kontekstu builda (docker compose)
# ==========================================
FROM rust:1-bookworm AS compile
WORKDIR /build
COPY Cargo.toml ./
COPY _src ./_src
RUN cargo build --release

# ==========================================
# 5. RUNNER
# ==========================================
FROM runtime-base AS runner
WORKDIR /app
COPY --from=compile /build/target/release/client_rust /app/client_rust
CMD ["/bin/sh", "-c", "sleep 2 && exec /app/client_rust"]
