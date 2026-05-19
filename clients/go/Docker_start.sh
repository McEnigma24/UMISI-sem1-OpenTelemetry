#!/usr/bin/env bash
set -euo pipefail
cd /workspace
mkdir -p docker-target
# Bez pobierania łańcucha narzędzi z sieci (go.mod „toolchain” → proxy.golang.org).
export GOTOOLCHAIN=local
# Bez `go mod download` / `go mod tidy` przy każdym buildzie (sieć + czas). Moduły są w volumenie Docker (docker_build.sh → /go/pkg/mod).
# Po zmianie zależności na hoście: `go mod tidy` (lokalnie), potem build kontenera.
exec go build -mod=readonly -trimpath -ldflags="-s -w" -o docker-target/client_go .
