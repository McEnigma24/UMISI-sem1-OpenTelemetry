#!/usr/bin/env bash
# Lista serwisów zapisanych w Jaegerze (Query API). Sprawdź, czy jest worker_nodejs.
#   chmod +x scripts/jaeger_list_services.sh
#   ./scripts/jaeger_list_services.sh
#   JAEGER_URL=http://127.0.0.1:16686 ./scripts/jaeger_list_services.sh
set -euo pipefail
BASE="${JAEGER_URL:-http://127.0.0.1:16686}"
URL="${BASE}/api/services"
code="$(curl -sS -o /tmp/jaeger_services.json -w '%{http_code}' -m 10 "$URL" || true)"
echo "GET $URL -> HTTP $code"
if [[ "$code" != "200" ]]; then
  head -c 400 /tmp/jaeger_services.json 2>/dev/null || true
  echo >&2
  exit 2
fi
if command -v jq >/dev/null 2>&1; then
  jq -r '.data[]?' /tmp/jaeger_services.json 2>/dev/null | sort -u
else
  cat /tmp/jaeger_services.json
fi
rm -f /tmp/jaeger_services.json
