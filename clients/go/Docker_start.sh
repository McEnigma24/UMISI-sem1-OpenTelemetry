#!/usr/bin/env bash
set -euo pipefail
cd /workspace
mkdir -p docker-target
# Brak go.sum na hoście / GOFLAGS=-mod=readonly → „missing go.sum entry”. Tidy + -mod=mod uzupełniają sumy w volumenie.
go mod download
go mod tidy
exec go build -mod=mod -trimpath -ldflags="-s -w" -o docker-target/client_go .
