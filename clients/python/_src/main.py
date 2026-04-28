#!/usr/bin/env python3
"""
Pipeline HTTP: POST /v1/pipeline (JSON) — counter + table_of_clients, forward do DEMO_NEXT_URL.

OTLP: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, opcj. metryki: OTEL_EXPORTER_OTLP_METRICS_ENDPOINT
  (domyślnie ten sam host co trace + /v1/metrics).

Demo ENV:
  DEMO_HTTP_ADDR        — host:port (domyślnie 0.0.0.0:8080)
  DEMO_HTTP_PATH        — (domyślnie /v1/pipeline)
  DEMO_CLIENT_ID        — id w table_of_clients (domyślnie py)
  DEMO_NEXT_URL         — pełny URL następnego węzła; pusty = terminal (u Python zawsze forward)
Resource: OTEL_*, patrz _build_resource()
"""
from __future__ import annotations

import json
import os
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

# (Pełne legacy _otel_api_exercises: patrz git history / demo_mode poniżej.)

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


def _process_payload(data: dict[str, Any], client_id: str) -> dict[str, Any]:
    c = int(data.get("counter", "0"))
    table = list(data.get("table_of_clients", []))
    c += 1
    table = table + [client_id]
    return {"counter": str(c), "table_of_clients": table}


def _forward_to_next(url: str, body: bytes) -> tuple[int, bytes]:
    carrier: dict[str, str] = {}
    inject(carrier)
    h = {**carrier, "Content-Type": "application/json", "User-Agent": "demo-pipeline/python"}
    req = urlrequest.Request(url, data=body, method="POST", headers=h)
    with urlrequest.urlopen(req, timeout=30) as resp:
        return resp.getcode(), resp.read()


def _make_handler(
    path: str, client_id: str, next_url: str | None
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

            py_line(
                f"[{client_id}] received: {json.dumps(data, ensure_ascii=False)}"
            )

            carrier = _lower_headers(self.headers)
            parent_ctx = extract(carrier)
            token = otel_context.attach(parent_ctx)
            try:
                with tr.start_as_current_span(
                    "pipeline.hop",
                    attributes={
                        "demo.client_id": client_id,
                        "demo.has_forward": next_url is not None and next_url != "",
                    },
                ) as span:
                    out = _process_payload(data, client_id)
                    body_out = json.dumps(out).encode("utf-8")
                    if next_url and next_url.strip():
                        py_line(
                            f"[{client_id}] forward to {next_url.strip()}: "
                            f"{json.dumps(out, ensure_ascii=False)}"
                        )
                    else:
                        py_line(
                            f"[{client_id}] respond (terminal): "
                            f"{json.dumps(out, ensure_ascii=False)}"
                        )
                    span.set_attribute("demo.counter", out["counter"])
                    span.set_attribute("demo.table_len", len(out["table_of_clients"]))
                    if next_url and next_url.strip():
                        with tr.start_as_current_span(
                            "pipeline.forward",
                            kind=trace.SpanKind.CLIENT,
                            attributes={"http.url": next_url},
                        ):
                            try:
                                code, resp_body = _forward_to_next(next_url.strip(), body_out)
                            except (urlerror.URLError, OSError) as e:
                                span.record_exception(e)
                                self.send_error(502, f"forward failed: {e}")
                                return
                        span.set_attribute("demo.downstream_status", code)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.end_headers()
                        self.wfile.write(resp_body)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body_out)))
                        self.end_headers()
                        self.wfile.write(body_out)
                    msg_counter.add(1, {"client_id": client_id})
            finally:
                otel_context.detach(token)
                hop_hist.record(
                    (time.perf_counter() - t0) * 1000.0, {"client_id": client_id}
                )

    return Handler


def run_server() -> None:
    addr = os.environ.get("DEMO_HTTP_ADDR", "0.0.0.0:8080")
    host, _, port_s = addr.rpartition(":")
    port = int(port_s or "8080")
    path = os.environ.get("DEMO_HTTP_PATH", "/v1/pipeline")
    client_id = os.environ.get("DEMO_CLIENT_ID", "py").strip() or "py"
    next_url = os.environ.get("DEMO_NEXT_URL", "").strip() or None
    if not next_url:
        py_line("DEMO_NEXT_URL pusty: węzeł terminalny (odpowiedź = przetworzony JSON, brak forward).")

    provider = _init_telemetry()
    if _TRACER is None:
        raise SystemExit(1)
    hcls = _make_handler(path, client_id, next_url)
    httpd = ThreadingHTTPServer((host if host else "0.0.0.0", port), hcls)
    py_line(f"pipeline: listen http://{addr}{path} client_id={client_id!r} next={next_url!r}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=10_000)


def main() -> int:
    # Domyślnie: tryb serwera pipeline. Legacy: export DEMO_MODE=exercises
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
