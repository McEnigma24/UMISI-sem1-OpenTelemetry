#!/usr/bin/env python3
"""
Klient zewnętrzny (bez OTLP): POST na gateway — pojedynczy request lub scenariusz z pliku.

Tryb 1 — pojedynczy request (domyślny gdy brak DEMO_SCENARIO_FILE):
  DEMO_TARGET_URL, DEMO_PAYLOAD_FILE (opcj.), DEMO_CLIENT_LOG

Tryb 2 — scenariusz (meta-level):
  DEMO_SCENARIO_FILE lub pierwszy argument argv — JSON z ``groups[]``:
    - initial_delay_sec, periodicity_sec (sekundy między falami w grupie)
    - waves — ile fal wykonać (null lub brak = w nieskończoność, dopóki globalny stop)
    - send.mode: sequential | parallel (w jednej fali: kolejno vs równolegle)
    - send.payload_files — lista plików z payloadem API (względem katalogu scenariusza)
  defaults: target_url, client_log, timeout_sec
  stop (opcjonalnie): after_duration_sec / duration_sec, after_total_requests / total_requests
    — jeśli podane, uruchamiany jest monitor kończący wszystkie grupy.
  Grupy działają równolegle (osobne asyncio task). SIGINT/SIGTERM zatrzymują.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _line(msg: str) -> None:
    now = datetime.now()
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts} {msg}", flush=True)


def _default_payload() -> dict[str, Any]:
    return {
        "route": [
            {"id": "py", "visited": False, "processing_time": "0.05s"},
            {"id": "rs", "visited": False, "processing_time": "0.05s"},
            {"id": "cs", "visited": False, "processing_time": "0.05s"},
        ],
        "visit_log": [],
        "counter": "0",
        "table_of_clients": [],
    }


def _load_payload_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError("payload JSON must be an object")
    return obj


def _sync_post(
    url: str,
    payload: dict[str, Any],
    timeout_sec: float,
    log_path: str,
    detail_prefix: str,
) -> tuple[int, float, str]:
    """Zwraca (http_code_or_-1, duration_ms, krótki opis)."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "demo-external-client/2-scenario",
        },
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            out = resp.read()
            code = resp.getcode()
            text = out.decode("utf-8", errors="replace")
            dur_ms = (time.perf_counter() - t0) * 1000.0
            _append_log(
                log_path,
                url,
                code,
                dur_ms,
                f"{detail_prefix} body_len={len(text)}",
            )
            return code, dur_ms, f"HTTP {code} {len(text)}B"
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        dur_ms = (time.perf_counter() - t0) * 1000.0
        _append_log(log_path, url, e.code, dur_ms, f"{detail_prefix} err={err[:300]}")
        return e.code, dur_ms, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        dur_ms = (time.perf_counter() - t0) * 1000.0
        _append_log(log_path, url, -1, dur_ms, f"{detail_prefix} {e}")
        return -1, dur_ms, str(e)


def _append_log(path: str, url: str, status: int, dur_ms: float, detail: str) -> None:
    now = datetime.now().isoformat(timespec="milliseconds")
    line = f"{now}\t{dur_ms:.1f}ms\tstatus={status}\turl={url}\t{detail}\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def main() -> int:
    scenario_path = os.environ.get("DEMO_SCENARIO_FILE", "").strip()
    if not scenario_path and len(sys.argv) > 1:
        scenario_path = sys.argv[1].strip()
    if scenario_path:
        return asyncio.run(run_scenario(Path(scenario_path)))
    return run_single()


def run_single() -> int:
    url = os.environ.get(
        "DEMO_TARGET_URL", "http://127.0.0.1:18080/v1/pipeline"
    )
    log_path = os.environ.get("DEMO_CLIENT_LOG", "client_e2e.log").strip()
    pf = os.environ.get("DEMO_PAYLOAD_FILE", "").strip()

    try:
        if pf:
            payload = _load_payload_file(Path(pf))
        else:
            payload = _default_payload()
    except OSError as e:
        print(f"payload file error: {e}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        print(f"invalid payload: {e}", file=sys.stderr)
        return 1

    code, dur_ms, msg = _sync_post(url, payload, 120.0, log_path, "single")
    if code == 200:
        _line(f"e2e ok {dur_ms:.1f} ms {msg}")
        return 0
    print(msg, file=sys.stderr)
    return 1


@dataclass
class RunState:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.perf_counter)
    total_requests: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def run_scenario(scenario_file: Path) -> int:
    try:
        raw = scenario_file.read_text(encoding="utf-8")
        scen = json.loads(raw)
    except OSError as e:
        print(f"scenario file: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"invalid scenario JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(scen, dict):
        print("scenario root must be an object", file=sys.stderr)
        return 1

    base_dir = scenario_file.resolve().parent
    defaults = scen.get("defaults") if isinstance(scen.get("defaults"), dict) else {}
    stop_cfg = scen.get("stop") if isinstance(scen.get("stop"), dict) else {}
    groups = scen.get("groups")
    if not isinstance(groups, list) or not groups:
        print("scenario.groups must be a non-empty array", file=sys.stderr)
        return 1

    def_url = (
        str(defaults.get("target_url") or "").strip()
        or os.environ.get("DEMO_TARGET_URL", "").strip()
        or "http://127.0.0.1:18080/v1/pipeline"
    )
    def_log = (
        str(defaults.get("client_log") or defaults.get("demo_client_log") or "").strip()
        or os.environ.get("DEMO_CLIENT_LOG", "client_e2e.log").strip()
    )
    timeout_sec = float(
        defaults.get("timeout_sec")
        or defaults.get("request_timeout_sec")
        or os.environ.get("DEMO_REQUEST_TIMEOUT_SEC", "120")
    )

    duration_sec = stop_cfg.get("after_duration_sec")
    if duration_sec is None:
        duration_sec = stop_cfg.get("duration_sec")
    total_cap = stop_cfg.get("after_total_requests")
    if total_cap is None:
        total_cap = stop_cfg.get("total_requests")

    dur_f: float | None = None
    if duration_sec is not None:
        dur_f = float(duration_sec)
    req_cap: int | None = None
    if total_cap is not None:
        req_cap = int(total_cap)

    state = RunState()
    loop = asyncio.get_running_loop()

    def _request_stop(*_: Any) -> None:
        _line("stop requested (signal lub global limit)")
        state.stop.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _request_stop)
        loop.add_signal_handler(signal.SIGTERM, _request_stop)
    except NotImplementedError:
        pass

    tasks = []
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            print(f"group[{i}] skipped: not an object", file=sys.stderr)
            continue
        t = asyncio.create_task(
            group_runner(
                g,
                base_dir,
                default_url=def_url,
                default_log=def_log,
                timeout_sec=timeout_sec,
                state=state,
                dur_limit_sec=dur_f,
                req_cap=req_cap,
                group_index=i,
            ),
            name=f"group-{i}",
        )
        tasks.append(t)

    if not tasks:
        print("no valid groups", file=sys.stderr)
        return 1

    need_monitor = dur_f is not None or req_cap is not None
    if need_monitor:
        monitor = asyncio.create_task(
            global_monitor(state, dur_f, req_cap), name="monitor"
        )
        tasks.append(monitor)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            print(f"task error: {r}", file=sys.stderr)
    return 0


async def global_monitor(
    state: RunState,
    dur_limit_sec: float | None,
    req_cap: int | None,
) -> None:
    """Ustawia stop po czasie lub liczbie żądań."""
    try:
        while not state.stop.is_set():
            if dur_limit_sec is not None:
                if time.perf_counter() - state.started_at >= dur_limit_sec:
                    _line(f"global stop: duration {dur_limit_sec}s reached")
                    state.stop.set()
                    return
            if req_cap is not None:
                async with state.lock:
                    if state.total_requests >= req_cap:
                        _line(f"global stop: total_requests {req_cap} reached")
                        state.stop.set()
                        return
            await asyncio.sleep(0.2)
    finally:
        state.stop.set()


def _resolve_files(base_dir: Path, paths: list[Any]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            raise ValueError("payload_files entries must be non-empty strings")
        path = Path(p.strip())
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        out.append(path)
    return out


async def group_runner(
    group: dict[str, Any],
    base_dir: Path,
    default_url: str,
    default_log: str,
    timeout_sec: float,
    state: RunState,
    dur_limit_sec: float | None,
    req_cap: int | None,
    group_index: int,
) -> None:
    name = str(group.get("name") or f"group{group_index}")
    url = str(group.get("target_url") or "").strip() or default_url
    log_path = str(group.get("client_log") or "").strip() or default_log

    initial = float(group.get("initial_delay_sec") or 0)
    periodicity = float(group.get("periodicity_sec") or group.get("every_sec") or 0)
    waves_limit = group.get("waves")
    if waves_limit is not None:
        waves_limit = int(waves_limit)

    send = group.get("send")
    if not isinstance(send, dict):
        _line(f"[{name}] missing send object — skipping")
        return

    mode = str(send.get("mode") or "sequential").strip().lower()
    if mode not in ("sequential", "parallel"):
        _line(f"[{name}] invalid send.mode {mode!r} — use sequential|parallel")
        return

    raw_files = send.get("payload_files")
    if not isinstance(raw_files, list) or not raw_files:
        _line(f"[{name}] send.payload_files must be a non-empty array")
        return

    try:
        files = _resolve_files(base_dir, raw_files)
    except ValueError as e:
        _line(f"[{name}] {e}")
        return

    for p in files:
        if not p.is_file():
            _line(f"[{name}] payload file not found: {p}")
            return

    _line(
        f"[{name}] start initial_delay={initial}s periodicity={periodicity}s "
        f"waves={waves_limit!r} mode={mode} files={[str(f) for f in files]}"
    )

    await asyncio.sleep(initial)

    wave_i = 0
    while not state.stop.is_set():
        if waves_limit is not None and wave_i >= waves_limit:
            _line(f"[{name}] done after {waves_limit} wave(s)")
            break

        if dur_limit_sec is not None and time.perf_counter() - state.started_at >= dur_limit_sec:
            break
        if req_cap is not None:
            async with state.lock:
                if state.total_requests >= req_cap:
                    break

        _line(f"[{name}] wave {wave_i + 1} begin")

        if mode == "sequential":
            for fi, fp in enumerate(files):
                if state.stop.is_set():
                    break
                async with state.lock:
                    if req_cap is not None and state.total_requests >= req_cap:
                        break
                payload = await asyncio.to_thread(_load_payload_file, fp)
                detail = f"g={name} wave={wave_i} seq={fi} file={fp.name}"
                code, dur_ms, msg = await asyncio.to_thread(
                    _sync_post, url, payload, timeout_sec, log_path, detail
                )
                async with state.lock:
                    state.total_requests += 1
                _line(f"[{name}] {detail} -> {msg} ({dur_ms:.1f} ms)")
        else:

            async def one_file(fp: Path, seq: int) -> None:
                payload = await asyncio.to_thread(_load_payload_file, fp)
                detail = f"g={name} wave={wave_i} par={seq} file={fp.name}"
                _code, dur_ms, msg = await asyncio.to_thread(
                    _sync_post, url, payload, timeout_sec, log_path, detail
                )
                async with state.lock:
                    state.total_requests += 1
                _line(f"[{name}] {detail} -> {msg} ({dur_ms:.1f} ms)")

            await asyncio.gather(*[one_file(fp, j) for j, fp in enumerate(files)])

        wave_i += 1

        if state.stop.is_set():
            break

        if periodicity > 0:
            await asyncio.sleep(periodicity)


if __name__ == "__main__":
    raise SystemExit(main())
