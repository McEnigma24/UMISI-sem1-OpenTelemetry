#!/usr/bin/env python3
"""
Pipeline HTTP: POST /v1/pipeline (JSON).

Payload musi zawierać niepustą listę ``route`` (pierwszy segment = gateway, np. ``py``):
  Każdy węzeł obsługuje pierwszy nieodwiedzony segment z ``id`` == DEMO_CLIENT_ID,
  symuluje pracę CPU (pętla obliczeń) przez niepustą listę ``processing_steps`` …
  (wspólny span ``pipeline.processing``, podspany o nazwie ``activity`` — jeden traceId).
  Opcja ``nested_route`` w elemencie ``processing_steps``: osobny POST na pierwszego workera
  (pierwszy ``id`` ≠ ``py``), propagacja W3C z bieżącego kroku; opcjonalne ``time`` = CPU przed nested.
  Oznacza visited, aktualizuje counter/table/visit_log, forward wg DEMO_PEER_* / DEMO_PEER_MAP.

OTLP: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, opcj. metryki i logi (``OTEL_EXPORTER_OTLP_LOGS_ENDPOINT``).

Demo ENV:
  DEMO_HTTP_ADDR, DEMO_HTTP_PATH, DEMO_CLIENT_ID
  DEMO_PEER_PY, DEMO_PEER_RS, DEMO_PEER_CS — URL pełny do pipeline (lub DEMO_PEER_MAP jako JSON obiekt id->url)
  DEMO_MAX_PROCESSING_SEC — limit czasu symulacji CPU (domyślnie 120)
  DEMO_PROCESS_METRICS — false/0: bez gauge'y demo.process.* (domyślnie włączone; punkty mają service.name + demo.client_id)
  OTEL_DEMO_LOG_EXPORT — off/0/false: bez eksportu logów OTLP (domyślnie otlp przy włączonym OTLP trace)
  PYROSCOPE_SERVER — np. ``http://pyroscope:4040`` → continuous profiling (stosy CPU) do Grafana Pyroscope; osobno od OTLP metryk.
  PYROSCOPE_ENABLED — false/0: nie startuj ``pyroscope-io`` mimo ustawionego ``PYROSCOPE_SERVER``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
from datetime import datetime
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import psutil

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject, set_global_textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

_TRACER: trace.Tracer | None = None
_METER: metrics.Meter | None = None
_LOG_PROVIDER: LoggerProvider | None = None


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def py_line(msg: str) -> None:
    now = datetime.now()
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    line = f"{ts} {msg}"
    print(line, flush=True)
    if _LOG_PROVIDER is not None:
        logging.getLogger("demo.pipeline").info(line)


def _build_resource() -> Resource:
    instance_id = os.environ.get("OTEL_SERVICE_INSTANCE_ID") or str(uuid.uuid4())
    env = os.environ.get("OTEL_ENVIRONMENT") or os.environ.get(
        "DEPLOYMENT_ENVIRONMENT", "local"
    )
    tag = os.environ.get("OTEL_DEMO_RESOURCE_TAG", "").strip()
    host = socket.gethostname()
    attrs: dict = {
        "service.name": "gateway_python",
        "service.version": "1.0.0",
        "service.instance.id": instance_id,
        "deployment.environment": env,
        "host.name": host,
        "telemetry.sdk.language": "python",
        "telemetry.sdk.name": "opentelemetry",
    }
    if tag:
        attrs["demo.instance.tag"] = tag
    return Resource.create(attrs)


def _otlp_traces_ep() -> str:
    return os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"
    )


def _otlp_metrics_ep() -> str:
    o = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if o:
        return o
    return _otlp_traces_ep().replace("/v1/traces", "/v1/metrics", 1)


def _otlp_logs_ep() -> str:
    o = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
    if o:
        return o
    return _otlp_traces_ep().replace("/v1/traces", "/v1/logs", 1)


def _use_otlp_http() -> bool:
    m = os.environ.get("OTEL_DEMO_TRACE_EXPORT", "")
    if not m:
        return True
    if m == "ostream":
        return False
    if m in ("otlp", "http"):
        return True
    return False


def _use_otlp_log_export() -> bool:
    if not _use_otlp_http():
        return False
    v = os.environ.get("OTEL_DEMO_LOG_EXPORT", "otlp").strip().lower()
    return v not in ("0", "false", "no", "off")


def _process_metrics_enabled() -> bool:
    v = os.environ.get("DEMO_PROCESS_METRICS", "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _pyroscope_push_enabled() -> bool:
    v = os.environ.get("PYROSCOPE_ENABLED", "true").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("PYROSCOPE_SERVER", "").strip())


def _init_pyroscope_push() -> None:
    """Continuous profiling → Pyroscope (Grafana Drilldown Profiles). Osobno od OTLP metryk ``demo.process.*``."""
    if not _pyroscope_push_enabled():
        return
    try:
        import pyroscope
    except ImportError:
        py_line("PYROSCOPE_SERVER set but pyroscope-io missing — rebuild image (requirements.txt)")
        return
    server = os.environ.get("PYROSCOPE_SERVER", "").strip()
    app_name = (os.environ.get("OTEL_SERVICE_NAME") or "").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_CLIENT_ID") or "py").strip() or "py"
    env = os.environ.get("OTEL_ENVIRONMENT") or os.environ.get(
        "DEPLOYMENT_ENVIRONMENT", "local"
    )
    pyroscope.configure(
        application_name=app_name,
        server_address=server,
        tags={
            "service.name": app_name,
            "demo.client_id": cid,
            "deployment.environment": env,
        },
    )
    py_line(
        f"Pyroscope push profiler: server='{server}' application_name='{app_name}'"
    )


def _process_metric_point_attributes() -> dict[str, str]:
    """Atrybuty punktu OTLP → osobne serie w Prometheusie (resource często nie trafia jako label)."""
    sn = (os.environ.get("OTEL_SERVICE_NAME") or "").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_CLIENT_ID") or "py").strip() or "py"
    return {"service.name": sn, "demo.client_id": cid}


def _cpu_spin_seconds(sec: float) -> None:
    """Symulacja pracy CPU do momentu ``deadline`` (zegar monotoniczny)."""
    if sec <= 0.0:
        return
    deadline = time.perf_counter() + sec
    v = 1
    while time.perf_counter() < deadline:
        for _ in range(1024):
            v = (v * 1103515245 + 12345) & 0x7FFFFFFF


def _register_process_metrics(meter: metrics.Meter) -> None:
    if not _process_metrics_enabled():
        return
    proc = psutil.Process()

    def _cpu_cb(_options: Any) -> Any:
        pct = float(proc.cpu_percent(interval=None))
        pct = min(100.0, max(0.0, pct))
        yield Observation(pct, _process_metric_point_attributes())

    def _mem_cb(_options: Any) -> Any:
        attrs = _process_metric_point_attributes()
        yield Observation(float(proc.memory_info().rss), attrs)

    meter.create_observable_gauge(
        name="demo.process.cpu.utilization",
        callbacks=[_cpu_cb],
        unit="%",
        description="Użycie CPU procesu 0–100 (psutil)",
    )
    meter.create_observable_gauge(
        name="demo.process.memory.usage",
        callbacks=[_mem_cb],
        unit="By",
        description="RSS procesu (bajty)",
    )


def _init_telemetry() -> TracerProvider:
    global _TRACER, _METER, _LOG_PROVIDER
    _LOG_PROVIDER = None
    # Jawny W3C tracecontext — spójnie z workerami (Rust); bez tego zależność od domyślnego OTEL_PROPAGATORS.
    set_global_textmap(TraceContextTextMapPropagator())
    resource = _build_resource()
    if _use_otlp_http():
        texp = OTLPSpanExporter(endpoint=_otlp_traces_ep(), timeout=5)
        provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
        provider.add_span_processor(SimpleSpanProcessor(texp))
        trace.set_tracer_provider(provider)
        mexp = OTLPMetricExporter(endpoint=_otlp_metrics_ep())
        reader = PeriodicExportingMetricReader(mexp, export_interval_millis=5000)
        mp = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(mp)
        if _use_otlp_log_export():
            lexp = OTLPLogExporter(endpoint=_otlp_logs_ep(), timeout=5)
            lp = LoggerProvider(resource=resource)
            lp.add_log_record_processor(BatchLogRecordProcessor(lexp))
            set_logger_provider(lp)
            _LOG_PROVIDER = lp
            dem = logging.getLogger("demo.pipeline")
            dem.setLevel(logging.INFO)
            dem.handlers.clear()
            dem.addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=lp))
            dem.propagate = False
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
        trace.set_tracer_provider(provider)
        mp = MeterProvider(resource=resource)
        metrics.set_meter_provider(mp)

    _TRACER = trace.get_tracer("gateway_python", "1.0.0")
    _METER = metrics.get_meter("gateway_python", "1.0.0")
    if _use_otlp_http() and _process_metrics_enabled():
        _register_process_metrics(_METER)
    return provider  # type: ignore[return-value]


def _lower_headers(d: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        if v is not None:
            out[k.lower()] = v
    return out


def _load_peer_map() -> dict[str, str]:
    raw = os.environ.get("DEMO_PEER_MAP", "").strip()
    if raw:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("DEMO_PEER_MAP must be a JSON object")
        return {str(k).strip().lower(): str(v).strip() for k, v in obj.items() if str(v).strip()}
    out: dict[str, str] = {}
    for key, val in os.environ.items():
        if not key.startswith("DEMO_PEER_") or key == "DEMO_PEER_MAP":
            continue
        tail = key[len("DEMO_PEER_") :].strip().lower()
        if tail and val.strip():
            out[tail] = val.strip()
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
    if first == "py":
        raise ValueError(
            f"processing_steps[{step_i}].nested_route[0].id must not be 'py' (workers only)"
        )
    return tuple(out)


def _nested_pipeline_payload(nested_route: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    nr: list[dict[str, Any]] = []
    for s in nested_route:
        s2 = dict(s)
        s2["visited"] = False
        nr.append(s2)
    return {
        "route": nr,
        "visit_log": [],
        "counter": "0",
        "table_of_clients": [],
    }


def _parse_processing_steps(
    seg: dict[str, Any],
) -> list[_ProcStepCpu | _ProcStepNested] | None:
    """Lista kroków: CPU albo ``nested_route`` (tylko workery; pierwszy ``id`` ≠ ``py``)."""
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


def _bump_counter_table(data: dict[str, Any], client_id: str) -> None:
    c = int(str(data.get("counter", "0")))
    table = list(data.get("table_of_clients", []))
    data["counter"] = str(c + 1)
    data["table_of_clients"] = table + [client_id]


def _forward_to_next(url: str, body: bytes) -> tuple[int, bytes]:
    carrier: dict[str, str] = {}
    inject(carrier)
    h = {**carrier, "Content-Type": "application/json", "User-Agent": "demo-pipeline/python"}
    req = urlrequest.Request(url, data=body, method="POST", headers=h)
    with urlrequest.urlopen(req, timeout=120) as resp:
        return resp.getcode(), resp.read()


def _make_handler(
    path: str,
    client_id: str,
    peer_map: dict[str, str],
) -> type[BaseHTTPRequestHandler]:
    tr = _TRACER
    meter = _METER
    if tr is None or meter is None:
        raise RuntimeError("telemetry not initialized")
    hop_hist = meter.create_histogram(
        "demo.pipeline.hop.duration_ms",
        unit="ms",
        description="Czas przetworzenia i ewent. forward jednego hopy",
    )
    msg_counter = meter.create_counter(
        "demo.pipeline.messages", description="Liczba przetworzonych wiadomości w węźle"
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:
            return

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

            py_line(f"[{client_id}] received: {json.dumps(data, ensure_ascii=False)}")

            carrier = _lower_headers(self.headers)
            parent_ctx = extract(carrier)
            token = otel_context.attach(parent_ctx)
            try:
                route = data.get("route")
                use_route = isinstance(route, list) and len(route) > 0

                if use_route:
                    self._handle_route(data, client_id, peer_map, tr, msg_counter, hop_hist, t0)
                else:
                    self.send_error(
                        400,
                        "JSON must include non-empty route array",
                    )
            finally:
                otel_context.detach(token)
                hop_hist.record(
                    (time.perf_counter() - t0) * 1000.0, {"client_id": client_id}
                )

        def _handle_route(
            self,
            data: dict[str, Any],
            client_id: str,
            peers: dict[str, str],
            tr: trace.Tracer,
            msg_counter: Any,
            _hop_hist: Any,
            _t0: float,
        ) -> None:
            route = data["route"]
            if not all(isinstance(x, dict) for x in route):
                self.send_error(400, "route must be a list of objects")
                return
            idx = _first_unvisited_index(route)
            if idx is None:
                self.send_error(400, "route has no unvisited segment")
                return
            seg = route[idx]
            seg_id = seg.get("id")
            if seg_id != client_id:
                self.send_error(
                    400,
                    "first unvisited route segment id must match this node "
                    f"(expected {client_id!r}, got {seg_id!r})",
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
                kind=trace.SpanKind.SERVER,
                attributes={
                    "demo.client_id": client_id,
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
                                nest_payload = _nested_pipeline_payload(st.nested_route)
                                nest_bytes = json.dumps(
                                    nest_payload, ensure_ascii=False
                                ).encode("utf-8")
                                py_line(
                                    f"[{client_id}] nested_forward to {nurl}: "
                                    f"{json.dumps(nest_payload, ensure_ascii=False)}"
                                )
                                with tr.start_as_current_span(
                                    "pipeline.nested_forward",
                                    kind=trace.SpanKind.CLIENT,
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
                vl.append(client_id)
                _bump_counter_table(data, client_id)

                next_idx = _first_unvisited_index(route)
                body_out = json.dumps(data, ensure_ascii=False).encode("utf-8")

                span.set_attribute("demo.counter", data.get("counter", ""))
                span.set_attribute(
                    "demo.table_len", len(data.get("table_of_clients", []) or [])
                )
                span.set_attribute("demo.has_forward", next_idx is not None)

                if next_idx is None:
                    py_line(
                        f"[{client_id}] respond (terminal route): "
                        f"{json.dumps(data, ensure_ascii=False)}"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body_out)))
                    self.end_headers()
                    self.wfile.write(body_out)
                    msg_counter.add(1, {"client_id": client_id})
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

                py_line(
                    f"[{client_id}] forward to {next_url}: "
                    f"{json.dumps(data, ensure_ascii=False)}"
                )
                with tr.start_as_current_span(
                    "pipeline.forward",
                    kind=trace.SpanKind.CLIENT,
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
                msg_counter.add(1, {"client_id": client_id})

    return Handler


def run_server() -> None:
    addr = os.environ.get("DEMO_HTTP_ADDR", "0.0.0.0:8080").strip()
    host, _, port_s = addr.rpartition(":")
    port = int(port_s or "8080")
    path = os.environ.get("DEMO_HTTP_PATH", "/v1/pipeline")
    client_id = os.environ.get("DEMO_CLIENT_ID", "py").strip() or "py"
    try:
        peer_map = _load_peer_map()
    except (json.JSONDecodeError, ValueError) as e:
        py_line(f"peer map config error: {e}")
        raise SystemExit(1) from e

    provider = _init_telemetry()
    if _TRACER is None:
        raise SystemExit(1)
    _init_pyroscope_push()
    hcls = _make_handler(path, client_id, peer_map)
    bind_host = host if host else "0.0.0.0"
    try:
        httpd = _ReusableThreadingHTTPServer((bind_host, port), hcls)
    except OSError as e:
        py_line(
            f"bind failed {addr!r} ({bind_host!r}:{port}): {e} — w kontenerze użyj 0.0.0.0:8080; "
            f"z hosta test: curl -sS -X POST http://127.0.0.1:18080{path} ..."
        )
        raise SystemExit(2) from e
    py_line(
        f"pipeline: listen http://{addr}{path} client_id={client_id!r} "
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
