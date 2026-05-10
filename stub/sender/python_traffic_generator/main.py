#!/usr/bin/env python3
"""
Jednorazowe uruchomienie: POST JSON na pierwszy węzeł łańcucha (typowo client-python).

DEMO_TARGET_URL — pełny URL (dom. http://client-python:8080/v1/pipeline w docker compose
  lub z hosta: http://127.0.0.1:18080/v1/pipeline gdy wystawiony port).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from datetime import datetime

def _line(msg: str) -> None:
    now = datetime.now()
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts} {msg}", flush=True)

def main() -> int:

    url = os.environ.get("DEMO_TARGET_URL", "http://127.0.0.1:18080/v1/pipeline")

    payload = {"counter": "0", "table_of_clients": []}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "demo-stub-sender/1"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = resp.read()
            text = out.decode("utf-8", errors="replace")
            print(text, file=sys.stdout)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {err}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
