# Bookworm LTS (zamiast experimental „trixie”). ``slim`` = mniejszy obraz build.
FROM rust:1-slim-bookworm AS build
WORKDIR /app
COPY . .
RUN cargo build --release --bin client_rust

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/target/release/client_rust /app/client_rust
CMD ["/bin/sh", "-c", "sleep 2 && exec /app/client_rust"]
