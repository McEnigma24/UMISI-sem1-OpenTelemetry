#!/usr/bin/env python3
"""
Punkt wejścia ``gateway-python``: wybór wersji kodu workera przez ``PYTHON_WORKER_MODE``.

- ``complete`` — ``workers/python_worker/_src`` (pełny OTel).
- ``incomplete`` — ``workers/python_worker_incomplete/_src`` (szablon labowy).

W obrazie Dockera oba warianty są w ``/app/modes/{complete|incomplete}``.
Lokalnie (poza kontenerem) ścieżki są względem korzenia repozytorium (katalog nad ``python_gateway/``).
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _normalize_mode(raw: str | None) -> str:
    m = (raw or "complete").strip().lower()
    if m in ("complete", "full", "prod", "1", "yes", "true"):
        return "complete"
    if m in ("incomplete", "lab", "partial", "0", "no", "false"):
        return "incomplete"
    print(
        f"PYTHON_WORKER_MODE={raw!r} nieznane — używam 'complete'. "
        f"Dozwolone: complete | incomplete",
        file=sys.stderr,
        flush=True,
    )
    return "complete"


def _mode_src_dir(mode: str) -> str:
    """Katalog z ``telemetry.py`` + ``pipeline_server.py`` dla danego trybu."""
    container = f"/app/modes/{mode}"
    if os.path.isfile(os.path.join(container, "pipeline_server.py")):
        return container
    here = Path(__file__).resolve().parent
    if here.name == "python_gateway":
        repo_root = here.parent
        if mode == "incomplete":
            d = repo_root / "workers" / "python_worker_incomplete" / "_src"
        else:
            d = repo_root / "workers" / "python_worker" / "_src"
        if not d.is_dir():
            raise RuntimeError(f"Brak katalogu źródeł workera: {d}")
        return str(d.resolve())
    raise RuntimeError(
        f"Brak pipeline_server dla trybu {mode!r}: w obrazie oczekiwano "
        f"{container}/pipeline_server.py (sprawdź COPY w Dockerfile; przy podejrzeniu pustego cache: "
        f"`docker compose build --no-cache gateway-python`). Katalog skryptu: {here}"
    )


def _apply_worker_mode() -> str:
    mode = _normalize_mode(os.environ.get("PYTHON_WORKER_MODE"))
    os.environ["PYTHON_WORKER_MODE"] = mode
    src = _mode_src_dir(mode)
    while src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)
    print(f"[gateway-python] PYTHON_WORKER_MODE={mode!r} src={src!r}", flush=True)
    return mode


def _run() -> int:
    _apply_worker_mode()
    from pipeline_server import main as pipeline_main  # noqa: E402

    return pipeline_main()


if __name__ == "__main__":
    try:
        raise SystemExit(_run())
    except SystemExit as e:
        if e.code not in (0, None):
            print(f"[gateway-python] exit code={e.code!r}", file=sys.stderr, flush=True)
        raise
    except BaseException:
        print("[gateway-python] uncaught exception:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1) from None
