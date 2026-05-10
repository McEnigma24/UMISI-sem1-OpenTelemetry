#!/usr/bin/env python3
"""
Pipeline HTTP: POST /v1/pipeline (JSON).

Payload musi zawierać niepustą listę ``route`` (pierwszy segment = gateway, np. ``py``):
  Każdy węzeł obsługuje pierwszy nieodwiedzony segment z ``id`` == DEMO_CLIENT_ID,
  symuluje ``processing_time``, oznacza visited, aktualizuje counter/table/visit_log,
  forward do następnego id wg mapy DEMO_PEER_* / DEMO_PEER_MAP.

OTLP: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, opcj. metryki.

Demo ENV:
  DEMO_HTTP_ADDR, DEMO_HTTP_PATH, DEMO_CLIENT_ID
  DEMO_PEER_PY, DEMO_PEER_RS, DEMO_PEER_CS — URL pełny do pipeline (lub DEMO_PEER_MAP jako JSON obiekt id->url)
  DEMO_MAX_PROCESSING_SEC — opcjonalny limit czasu snu (domyślnie 120)
"""
from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

_TRACER: trace.Tracer | None = None
_METER: metrics.Meter | None = None


def py_line(msg: str) -> None:
    now = datetime.now()
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts} {msg}", flush=True)


def _build_resource() -> Resource:
    instance_id = os.environ.get("OTEL_SERVICE_INSTANCE_ID") or str(uuid.uuid4())
    env = os.environ.get("OTEL_ENVIRONMENT") or os.environ.get(
        "DEPLOYMENT_ENVIRONMENT", "local"
    )
    tag = os.environ.get("OTEL_DEMO_RESOURCE_TAG", "").strip()
    host = socket.gethostname()
    attrs: dict = {
        "service.name": "demo_app",
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


def _use_otlp_http() -> bool:
    m = os.environ.get("OTEL_DEMO_TRACE_EXPORT", "")
    if not m:
        return True
    if m == "ostream":
        return False
    if m in ("otlp", "http"):
        return True
    return False


def _init_telemetry() -> TracerProvider:
    global _TRACER, _METER
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
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
        trace.set_tracer_provider(provider)
        mp = MeterProvider(resource=resource)
        metrics.set_meter_provider(mp)

    _TRACER = trace.get_tracer("demo_app", "1.0.0")
    _METER = metrics.get_meter("demo_app", "1.0.0")
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
            try:
                dur = _parse_processing_time(seg.get("processing_time"))
            except ValueError as e:
                self.send_error(400, str(e))
                return

            with tr.start_as_current_span(
                "pipeline.hop",
                attributes={
                    "demo.client_id": client_id,
                },
            ) as span:
                if dur > 0:
                    span.set_attribute("demo.simulated_processing_sec", dur)
                    with tr.start_as_current_span(
                        "pipeline.simulated_work",
                        attributes={"demo.sleep_sec": dur},
                    ):
                        time.sleep(dur)

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
    addr = os.environ.get("DEMO_HTTP_ADDR", "0.0.0.0:8080")
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
    hcls = _make_handler(path, client_id, peer_map)
    httpd = ThreadingHTTPServer((host if host else "0.0.0.0", port), hcls)
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
    if os.environ.get("DEMO_MODE", "pipeline").lower() == "exercises":
        return _main_legacy()
    run_server()
    return 0


def _main_legacy() -> int:
    py_line("DEMO_MODE=exercises: legacy demo (skrót). Pełne ćwiczenia OpenTelemetry: historia gita / wcześniejsza wersja pliku.")
    p = _init_telemetry()
    t = trace.get_tracer("demo_app", "1.0.0")
    with t.start_as_current_span("legacy_demo_outlined"):
        py_line("OpenTelemetry: legacy run (użyj DEMO_MODE=pipeline dla łańcucha HTTP).")
    pr = trace.get_tracer_provider()
    if hasattr(pr, "force_flush"):
        pr.force_flush(timeout_millis=10_000)  # type: ignore[union-attr]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
