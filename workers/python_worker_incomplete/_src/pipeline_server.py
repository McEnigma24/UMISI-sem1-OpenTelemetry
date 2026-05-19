#!/usr/bin/env python3
"""
Pipeline HTTP: POST /v1/pipeline (JSON) — route, CPU, forward, nested.

Konfiguracja OpenTelemetry: moduł ``telemetry`` (``init_telemetry``, ``log_pipeline_line``, …).
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import telemetry


class _LabSpan:
    """No-op placeholder used by the incomplete lab version.

    LAB TODO: replace this with real OpenTelemetry spans from ``trace.get_tracer``.
    The call sites below show where SERVER, INTERNAL and CLIENT spans should be
    created and which attributes are useful to report.
    """

    def set_attribute(self, _key: str, _value: Any) -> None:
        return

    def record_exception(self, _exc: BaseException) -> None:
        return

    def set_status(self, _status: Any) -> None:
        # LAB TODO: set the span status to ERROR when reporting simulated HTTP 500.
        return


class _LabTracer:
    @contextmanager
    def start_as_current_span(self, name: str, **_kwargs: Any) -> Any:
        # LAB TODO: create a real span named ``name`` here.
        yield _LabSpan()


class _LabCounter:
    def add(self, _value: int, _attrs: dict[str, str] | None = None) -> None:
        # LAB TODO: replace with a real OTel counter, e.g. demo.pipeline.messages.
        return


class _LabHistogram:
    def record(self, _value: float, _attrs: dict[str, str] | None = None) -> None:
        # LAB TODO: replace with a real OTel histogram, e.g. demo.pipeline.hop.duration_ms.
        return


def _otel_extract_context_hint(_headers: Any) -> None:
    # LAB TODO: extract W3C trace context from HTTP headers and attach it before
    # creating the SERVER span for this request. Without this, every worker starts
    # its own trace instead of joining the distributed trace.
    return


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _cpu_spin_seconds(sec: float) -> None:
    """Symulacja pracy CPU do momentu ``deadline`` (zegar monotoniczny)."""
    if sec <= 0.0:
        return
    deadline = time.perf_counter() + sec
    v = 1
    while time.perf_counter() < deadline:
        for _ in range(1024):
            v = (v * 1103515245 + 12345) & 0x7FFFFFFF


def _lower_headers(d: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        if v is not None:
            out[k.lower()] = v
    return out


def _peer_id_from_env_suffix(tail: str) -> str:
    """``DEMO_PEER_PY_GATEWAY`` → ``py-gateway``; ``DEMO_PEER_RS`` → ``rs``. ``DEMO_PEER_PY`` jest zabronione."""
    t = tail.strip().lower()
    if t == "py":
        raise ValueError(
            "DEMO_PEER_PY is no longer supported; use DEMO_PEER_PY_GATEWAY and DEMO_PEER_PY_WORKER "
            "(route ids py-gateway / py-worker)."
        )
    if t.startswith("py_"):
        return t.replace("_", "-")
    return t


def _normalize_peer_map_key(k: str) -> str:
    return _peer_id_from_env_suffix(str(k).strip().lower())


def _load_peer_map() -> dict[str, str]:
    raw = os.environ.get("DEMO_PEER_MAP", "").strip()
    if raw:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("DEMO_PEER_MAP must be a JSON object")
        return {
            _normalize_peer_map_key(k): str(v).strip()
            for k, v in obj.items()
            if str(v).strip()
        }
    out: dict[str, str] = {}
    for key, val in os.environ.items():
        if not key.startswith("DEMO_PEER_") or key == "DEMO_PEER_MAP":
            continue
        tail = key[len("DEMO_PEER_") :].strip().lower()
        if tail and val.strip():
            out[_peer_id_from_env_suffix(tail)] = val.strip()
    return out


_RE_SEC = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*s\s*$", re.I)
_RE_MS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*ms\s*$", re.I)


def _parse_processing_time(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        if v < 0:
            raise ValueError("processing_time must be non-negative")
        return float(v)
    if not isinstance(v, str):
        raise ValueError("processing_time must be a string or number")
    s = v.strip()
    if not s:
        return 0.0
    max_sec = float(os.environ.get("DEMO_MAX_PROCESSING_SEC", "120"))
    sec: float
    m = _RE_SEC.match(s)
    if m:
        sec = float(m.group(1))
    else:
        m2 = _RE_MS.match(s)
        if m2:
            sec = float(m2.group(1)) / 1000.0
        else:
            raise ValueError(f"invalid processing_time: {v!r} (use e.g. 5.6s or 100ms)")
    if sec < 0:
        raise ValueError("processing_time must be non-negative")
    if sec > max_sec:
        sec = max_sec
    return sec


def _parse_http_error_probability(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        raise ValueError("http_error_probability must be a number between 0.0 and 1.0")
    if isinstance(v, (int, float)):
        p = float(v)
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        try:
            p = float(s)
        except ValueError as e:
            raise ValueError(
                "http_error_probability must be a number between 0.0 and 1.0"
            ) from e
    else:
        raise ValueError("http_error_probability must be a number between 0.0 and 1.0")
    if p < 0.0 or p > 1.0:
        raise ValueError("http_error_probability must be between 0.0 and 1.0")
    return p


def _effective_http_error_probability(data: dict[str, Any], seg: dict[str, Any]) -> float:
    if "http_error_probability" in seg:
        return _parse_http_error_probability(seg.get("http_error_probability"))
    return _parse_http_error_probability(data.get("http_error_probability"))


def _span_name_for_activity(activity: str, index: int) -> str:
    a = (activity or "").strip()
    if not a:
        return "pipeline.processing_step"
    return a[:256]


def _segment_schema_error_or_none(seg: dict[str, Any], where: str) -> str | None:
    if "processing_time" in seg:
        return (
            f"{where}: field 'processing_time' is not supported; use non-empty "
            "'processing_steps' with {{\"activity\",\"time\"}} "
            "(one element for a single CPU block)"
        )
    raw = seg.get("processing_steps")
    if not isinstance(raw, list) or len(raw) == 0:
        return f"{where}: non-empty 'processing_steps' is required"
    return None


@dataclass(frozen=True)
class _ProcStepCpu:
    activity: str
    sec: float
    index: int


@dataclass(frozen=True)
class _ProcStepNested:
    activity: str
    pre_sec: float
    nested_route: tuple[dict[str, Any], ...]
    index: int


def _validate_nested_route_segments(raw: list[Any], step_i: int) -> tuple[dict[str, Any], ...]:
    if not raw:
        raise ValueError(f"processing_steps[{step_i}].nested_route must be a non-empty array")
    out: list[dict[str, Any]] = []
    for j, seg in enumerate(raw):
        if not isinstance(seg, dict):
            raise ValueError(
                f"processing_steps[{step_i}].nested_route[{j}] must be an object"
            )
        sid = seg.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError(
                f"processing_steps[{step_i}].nested_route[{j}].id must be a non-empty string"
            )
        sch = _segment_schema_error_or_none(
            seg, f"processing_steps[{step_i}].nested_route[{j}]"
        )
        if sch:
            raise ValueError(sch)
        out.append(seg)
    first = (out[0].get("id") or "").strip().lower()
    if first in ("py", "py-gateway"):
        raise ValueError(
            f"processing_steps[{step_i}].nested_route[0].id must not be {first!r} "
            "(nested subtree must start on a worker, not the public Python gateway)"
        )
    return tuple(out)


def _nested_pipeline_payload(
    nested_route: tuple[dict[str, Any], ...], root_http_error_probability: Any
) -> dict[str, Any]:
    nr: list[dict[str, Any]] = []
    for s in nested_route:
        s2 = dict(s)
        s2["visited"] = False
        nr.append(s2)
    return {
        "route": nr,
        "visit_log": [],
        "counter": "0",
        "table_of_workers": [],
        "http_error_probability": _parse_http_error_probability(root_http_error_probability),
    }


def _parse_processing_steps(
    seg: dict[str, Any],
) -> list[_ProcStepCpu | _ProcStepNested] | None:
    """Lista kroków: CPU albo ``nested_route`` (pierwszy ``id`` ≠ ``py-gateway`` / legacy ``py``)."""
    raw = seg.get("processing_steps")
    if not isinstance(raw, list) or len(raw) == 0:
        return None
    out: list[_ProcStepCpu | _ProcStepNested] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"processing_steps[{i}] must be an object")
        act_raw = item.get("activity")
        act = act_raw if isinstance(act_raw, str) else f"step_{i}"
        nr = item.get("nested_route")
        if nr is not None:
            if not isinstance(nr, list):
                raise ValueError(f"processing_steps[{i}].nested_route must be an array")
            validated = _validate_nested_route_segments(nr, i)
            try:
                pre = _parse_processing_time(item.get("time"))
            except ValueError as e:
                raise ValueError(f"processing_steps[{i}].time: {e}") from e
            out.append(
                _ProcStepNested(
                    activity=act, pre_sec=pre, nested_route=validated, index=i
                )
            )
        else:
            try:
                sec = _parse_processing_time(item.get("time"))
            except ValueError as e:
                raise ValueError(f"processing_steps[{i}].time: {e}") from e
            out.append(_ProcStepCpu(activity=act, sec=sec, index=i))
    return out


def _peer_url(peers: dict[str, str], seg_id: str) -> str | None:
    nid = seg_id.strip().lower()
    return peers.get(nid) or peers.get(seg_id.strip())


def _first_unvisited_index(route: list[Any]) -> int | None:
    for i, seg in enumerate(route):
        if not isinstance(seg, dict):
            continue
        if not seg.get("visited"):
            return i
    return None


def _bump_counter_table(data: dict[str, Any], worker_id: str) -> None:
    c = int(str(data.get("counter", "0")))
    table = list(data.get("table_of_workers", []))
    data["counter"] = str(c + 1)
    data["table_of_workers"] = table + [worker_id]


def _forward_to_next(url: str, body: bytes) -> tuple[int, bytes]:
    """HTTP forward without OTel propagation in the incomplete lab version."""
    # LAB TODO: inject W3C trace context into a carrier before sending the request.
    # The complete implementation must send ``traceparent`` from the active CLIENT
    # span (``pipeline.forward`` / ``pipeline.nested_forward``), otherwise the next
    # worker receives the HTTP request but starts a new trace.
    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "demo-pipeline/python-lab")
    with urlrequest.urlopen(req, timeout=120) as resp:
        return resp.getcode(), resp.read()


def _make_handler(
    path: str,
    worker_id: str,
    peer_map: dict[str, str],
) -> type[BaseHTTPRequestHandler]:
    tr = _LabTracer()
    # LAB TODO: create real OTel meter instruments here:
    # - histogram: demo.pipeline.hop.duration_ms
    # - counter: demo.pipeline.messages
    hop_hist = _LabHistogram()
    msg_counter = _LabCounter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:
            return

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            t0 = time.perf_counter()
            if self.path != path and self.path.rstrip("/") != path.rstrip("/"):
                self.send_error(404, "Not Found")
                return
            n = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "Invalid JSON")
                return
            if not isinstance(data, dict):
                self.send_error(400, "JSON must be an object")
                return

            telemetry.log_pipeline_line(
                f"[{worker_id}] received: {json.dumps(data, ensure_ascii=False)}"
            )

            _otel_extract_context_hint(_lower_headers(self.headers))
            try:
                route = data.get("route")
                use_route = isinstance(route, list) and len(route) > 0

                if use_route:
                    self._handle_route(
                        data,
                        worker_id,
                        peer_map,
                        tr,
                        msg_counter,
                        hop_hist,
                        t0,
                    )
                else:
                    self.send_error(
                        400,
                        "JSON must include non-empty route array",
                    )
            finally:
                hop_hist.record(
                    (time.perf_counter() - t0) * 1000.0,
                    {
                        **telemetry.process_metric_point_attributes(),
                        "worker_id": worker_id,
                    },
                )

        def _handle_route(
            self,
            data: dict[str, Any],
            worker_id: str,
            peers: dict[str, str],
            tr: _LabTracer,
            msg_counter: Any,
            _hop_hist: Any,
            _t0: float,
        ) -> None:
            route = data["route"]
            if not all(isinstance(x, dict) for x in route):
                self.send_error(400, "route must be a list of objects")
                return
            for _i, seg0 in enumerate(route):
                if not isinstance(seg0, dict):
                    continue
                rid = seg0.get("id")
                if isinstance(rid, str) and rid.strip().lower() == "py":
                    self.send_error(
                        400,
                        "route segment id 'py' is not allowed; use py-gateway or py-worker",
                    )
                    return
            idx = _first_unvisited_index(route)
            if idx is None:
                self.send_error(400, "route has no unvisited segment")
                return
            seg = route[idx]
            seg_id = seg.get("id")
            if seg_id != worker_id:
                self.send_error(
                    400,
                    "first unvisited route segment id must match this node "
                    f"(expected {worker_id!r}, got {seg_id!r})",
                )
                return
            try:
                http_error_probability = _effective_http_error_probability(data, seg)
            except ValueError as e:
                self.send_error(400, str(e))
                return
            if http_error_probability > 0.0 and random.random() < http_error_probability:
                with tr.start_as_current_span(
                    "pipeline.hop",
                    kind="SERVER",
                    attributes={
                        "demo.worker_id": worker_id,
                        "demo.simulated_http_error": True,
                        "demo.http_error_probability": http_error_probability,
                    },
                ) as span:
                    # LAB TODO: after implementing real OTel, mark this SERVER span as ERROR
                    # and keep these attributes so simulated failures are visible in Jaeger.
                    self._send_json(
                        500,
                        {
                            "error": "simulated_http_error",
                            "worker_id": worker_id,
                            "probability": http_error_probability,
                        },
                    )
                return
            sch = _segment_schema_error_or_none(seg, "route segment")
            if sch:
                self.send_error(400, sch)
                return
            try:
                steps = _parse_processing_steps(seg)
            except ValueError as e:
                self.send_error(400, str(e))
                return
            if steps is None:
                self.send_error(400, "route segment: non-empty 'processing_steps' is required")
                return

            with tr.start_as_current_span(
                "pipeline.hop",
                kind="SERVER",
                attributes={
                    "demo.worker_id": worker_id,
                },
            ) as span:
                cpu_sum = 0.0
                has_nested = False
                for st in steps:
                    if isinstance(st, _ProcStepNested):
                        has_nested = True
                        cpu_sum += st.pre_sec
                    else:
                        cpu_sum += st.sec
                span.set_attribute("demo.simulated_processing_sec", cpu_sum)
                span.set_attribute("demo.processing.mode", "steps")
                span.set_attribute("demo.step_count", len(steps))
                span.set_attribute("demo.has_nested_steps", has_nested)
                with tr.start_as_current_span(
                    "pipeline.processing",
                    attributes={
                        "demo.processing.mode": "steps",
                        "demo.step_count": len(steps),
                        "demo.simulated_processing_sec": cpu_sum,
                        "demo.has_nested_steps": has_nested,
                    },
                ):
                    for st in steps:
                        if isinstance(st, _ProcStepCpu):
                            name = _span_name_for_activity(st.activity, st.index)
                            with tr.start_as_current_span(
                                name,
                                attributes={
                                    "demo.activity": st.activity,
                                    "demo.cpu_spin_sec": st.sec,
                                    "demo.step_index": st.index,
                                },
                            ):
                                if st.sec > 0:
                                    _cpu_spin_seconds(st.sec)
                        else:
                            name = _span_name_for_activity(st.activity, st.index)
                            with tr.start_as_current_span(
                                name,
                                attributes={
                                    "demo.activity": st.activity,
                                    "demo.cpu_spin_sec": st.pre_sec,
                                    "demo.step_index": st.index,
                                    "demo.nested_subpipeline": True,
                                },
                            ):
                                if st.pre_sec > 0:
                                    _cpu_spin_seconds(st.pre_sec)
                                first_id = (
                                    st.nested_route[0].get("id") or ""
                                ).strip()
                                if not first_id:
                                    self.send_error(
                                        400,
                                        f"processing_steps[{st.index}].nested_route[0].id empty",
                                    )
                                    return
                                nurl = _peer_url(peers, first_id)
                                if not nurl:
                                    self.send_error(
                                        502,
                                        f"no peer URL for nested id {first_id!r}",
                                    )
                                    return
                                nest_payload = _nested_pipeline_payload(
                                    st.nested_route, data.get("http_error_probability")
                                )
                                nest_bytes = json.dumps(
                                    nest_payload, ensure_ascii=False
                                ).encode("utf-8")
                                telemetry.log_pipeline_line(
                                    f"[{worker_id}] nested_forward to {nurl}: "
                                    f"{json.dumps(nest_payload, ensure_ascii=False)}"
                                )
                                with tr.start_as_current_span(
                                    "pipeline.nested_forward",
                                    kind="CLIENT",
                                    attributes={"http.url": nurl.strip()},
                                ) as nfw:
                                    try:
                                        ncode, _nbody = _forward_to_next(
                                            nurl.strip(), nest_bytes
                                        )
                                    except (urlerror.URLError, OSError) as e:
                                        nfw.record_exception(e)
                                        self.send_error(
                                            502, f"nested_forward failed: {e}"
                                        )
                                        return
                                    nfw.set_attribute(
                                        "demo.downstream_status", ncode
                                    )
                                    if ncode < 200 or ncode >= 300:
                                        self.send_error(
                                            502,
                                            f"nested_forward HTTP {ncode}",
                                        )
                                        return

                seg["visited"] = True
                vl = data.setdefault("visit_log", [])
                if not isinstance(vl, list):
                    vl = []
                    data["visit_log"] = vl
                vl.append(worker_id)
                _bump_counter_table(data, worker_id)

                next_idx = _first_unvisited_index(route)
                body_out = json.dumps(data, ensure_ascii=False).encode("utf-8")

                span.set_attribute("demo.counter", data.get("counter", ""))
                span.set_attribute(
                    "demo.table_len", len(data.get("table_of_workers", []) or [])
                )
                span.set_attribute("demo.has_forward", next_idx is not None)

                if next_idx is None:
                    telemetry.log_pipeline_line(
                        f"[{worker_id}] respond (terminal route): "
                        f"{json.dumps(data, ensure_ascii=False)}"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body_out)))
                    self.end_headers()
                    self.wfile.write(body_out)
                    msg_counter.add(
                        1,
                        {
                            **telemetry.process_metric_point_attributes(),
                            "worker_id": worker_id,
                        },
                    )
                    return

                next_id = route[next_idx].get("id")
                if not isinstance(next_id, str) or not next_id.strip():
                    self.send_error(400, "invalid next route segment id")
                    return
                nid = next_id.strip().lower()
                next_url = peers.get(nid) or peers.get(next_id.strip())
                if not next_url:
                    self.send_error(502, f"no peer URL for id {next_id!r}")
                    return

                telemetry.log_pipeline_line(
                    f"[{worker_id}] forward to {next_url}: "
                    f"{json.dumps(data, ensure_ascii=False)}"
                )
                with tr.start_as_current_span(
                    "pipeline.forward",
                    kind="CLIENT",
                    attributes={"http.url": next_url},
                ) as fw:
                    try:
                        code, resp_body = _forward_to_next(next_url.strip(), body_out)
                    except (urlerror.URLError, OSError) as e:
                        fw.record_exception(e)
                        self.send_error(502, f"forward failed: {e}")
                        return
                    fw.set_attribute("demo.downstream_status", code)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                msg_counter.add(
                    1,
                    {
                        **telemetry.process_metric_point_attributes(),
                        "worker_id": worker_id,
                    },
                )

    return Handler


def run_server() -> None:
    addr = os.environ.get("DEMO_HTTP_ADDR", "0.0.0.0:8080").strip()
    host, _, port_s = addr.rpartition(":")
    port = int(port_s or "8080")
    path = os.environ.get("DEMO_HTTP_PATH", "/v1/pipeline")
    worker_id = os.environ.get("DEMO_WORKER_ID", "").strip()
    if not worker_id:
        raise SystemExit("DEMO_WORKER_ID is required (expected py-gateway or py-worker)")
    try:
        peer_map = _load_peer_map()
    except (json.JSONDecodeError, ValueError) as e:
        telemetry.log_pipeline_line(f"peer map config error: {e}")
        raise SystemExit(1) from e

    provider = telemetry.init_telemetry()
    # LAB TODO: initialize real OTel tracing/metrics/logs and Pyroscope here.
    # In the incomplete version this is intentionally a no-op so the HTTP
    # pipeline remains runnable before students add telemetry.
    telemetry.init_pyroscope_push()
    hcls = _make_handler(path, worker_id, peer_map)
    bind_host = host if host else "0.0.0.0"
    try:
        httpd = _ReusableThreadingHTTPServer((bind_host, port), hcls)
    except OSError as e:
        telemetry.log_pipeline_line(
            f"bind failed {addr!r} ({bind_host!r}:{port}): {e} — w kontenerze użyj 0.0.0.0:8080; "
            f"z hosta test: curl -sS -X POST http://127.0.0.1:18080{path} ..."
        )
        raise SystemExit(2) from e
    telemetry.log_pipeline_line(
        f"pipeline: listen http://{addr}{path} worker_id={worker_id!r} "
        f"peers={list(peer_map.keys())}"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=10_000)


def main() -> int:
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
