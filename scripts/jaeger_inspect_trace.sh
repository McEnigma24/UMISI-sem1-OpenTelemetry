#!/usr/bin/env bash
# Sprawdza JSON z Jaeger Query API: czy w trace są spany danego serwisu (np. worker_nodejs).
# Format odpowiedzi Jaeger: data[0].spans[] + processes{processID -> serviceName}.
#
# Użycie:
#   chmod +x scripts/jaeger_inspect_trace.sh
#   ./scripts/jaeger_inspect_trace.sh 318482eed3b2f14612ae26c743e7ec05
#   JAEGER_URL=http://127.0.0.1:16686 ./scripts/jaeger_inspect_trace.sh <trace_id> worker_nodejs
set -euo pipefail
TRACE_ID="${1:?podaj trace_id (32 znaki hex)}"
SERVICE="${2:-worker_nodejs}"
BASE="${JAEGER_URL:-http://127.0.0.1:16686}"
URL="${BASE}/api/traces/${TRACE_ID}"

if ! command -v curl >/dev/null 2>&1; then
  echo "brak curl" >&2
  exit 1
fi

tmp="$(mktemp)"
code="$(curl -sS -o "$tmp" -w '%{http_code}' -m 15 "$URL" || true)"
echo "GET $URL -> HTTP $code"
if [[ "$code" != "200" ]]; then
  head -c 800 "$tmp" 2>/dev/null || true
  echo >&2
  exit 2
fi

if command -v jq >/dev/null 2>&1; then
  dc="$(jq '.data | length' "$tmp" 2>/dev/null || echo 0)"
  if [[ "${dc:-0}" == "0" ]]; then
    echo "Jaeger zwrócił 200, ale .data jest puste (nieznany trace_id albo trace wygasły)." >&2
    rm -f "$tmp"
    exit 3
  fi
fi

echo "--- serwisy w trace (Jaeger process map) ---"
if command -v jq >/dev/null 2>&1; then
  jq -r '
    .data[0] as $t
    | ($t.processes // {}) as $p
    | [ $t.spans[] | ($p[.processID].serviceName // .process.serviceName // empty) ]
    | unique[]
    | select(length > 0)
  ' "$tmp" | sort -u

  echo "--- czy jest ${SERVICE}? ---"
  n="$(
    jq --arg s "$SERVICE" '
      .data[0] as $t
      | ($t.processes // {}) as $p
      | [ $t.spans[] | select(($p[.processID].serviceName // .process.serviceName) == $s) ]
      | length
    ' "$tmp"
  )"
  if [[ "${n:-0}" -gt 0 ]]; then
    echo "TAK: ${n} span(ów) z serwisem ${SERVICE}"
    jq -r --arg s "$SERVICE" '
      .data[0] as $t
      | ($t.processes // {}) as $p
      | $t.spans[]
      | select(($p[.processID].serviceName // .process.serviceName) == $s)
      | (
          .references // []
          | map(select(.refType == "CHILD_OF"))
          | .[0].spanID // "brak"
        ) as $parent
      | "  op=\(.operationName) spanID=\(.spanID) parent=\($parent)"
    ' "$tmp" | head -25
  else
    echo "NIE: w odpowiedzi API brak spanów z serwisem ${SERVICE}"
  fi
else
  echo "(zainstaluj jq dla czytelnego podsumowania)"
  grep -o "\"serviceName\":\"[^\"]*\"" "$tmp" | sort -u
  if grep -q "\"serviceName\":\"${SERVICE}\"" "$tmp"; then
    echo "TAK (grep): wystąpienie ${SERVICE} w JSON"
  else
    echo "NIE (grep): brak literalu serviceName=${SERVICE}"
  fi
fi

rm -f "$tmp"
