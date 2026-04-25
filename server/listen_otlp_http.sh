#!/usr/bin/env bash
# Prosty "słuchacz" TCP: netcat wypisuje to, co przyszło (POST + ewentualnie ciało OTLP/JSON).
# 1) Terminal A:  ./scripts/listen_otlp_http.sh   [port, domyślnie 4318]
# 2) Terminal B:  OTEL_DEMO_TRACE_EXPORT=otlp  ./start.sh
#     (dopasuj port:  export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:PORT/v1/traces  )
# Uwaga: goły nc nie wysyła odpowiedzi HTTP. Klient (libcurl w eksporcie OTLP) czeka na 200/timeout
#        — w logu aplikacji zobaczysz to jako opóźnienie lub błąd; surowe bajty i tak widać w tym oknie.

set -euo pipefail
clear
PORT=${1:-4318}
echo "Listening (netcat) on 0.0.0.0:${PORT} — one connection at a time" >&2

while true; do
  if nc -h 2>&1 | grep -q -- '-l.*-p' 2>/dev/null; then
    nc -l -p "${PORT}" || true
  else
    nc -l "${PORT}" || true
  fi
  echo ""
  echo ""
  echo "---- (next accept) ---" >&2
done
