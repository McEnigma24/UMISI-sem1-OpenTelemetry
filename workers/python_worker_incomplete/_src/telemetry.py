"""
Wersja **labowa** (niepełna): brak OTLP, brak Pyroscope, brak metryk procesu / logów OTLP.

API takie samo jak ``python_worker._src.telemetry`` — ``pipeline_server.py`` można kopiować 1:1.
Uzupełnij zgodnie z gotowcem: ``../python_worker/_src/telemetry.py``.
"""
from __future__ import annotations

import os
import socket
import sys
import uuid

from opentelemetry import metrics, trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACER: trace.Tracer | None = None
_METER: metrics.Meter | None = None


def log_pipeline_line(msg: str) -> None:
    """LAB: dodaj eksport logów OTLP (LoggerProvider + OTLPLogExporter) — zob. ``../python_worker/_src/telemetry.py``."""
    from datetime import datetime

    now = datetime.now()
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    line = f"{ts} {msg}"
    print(line, flush=True)


def _build_resource() -> Resource:
    instance_id = os.environ.get("OTEL_SERVICE_INSTANCE_ID") or str(uuid.uuid4())
    env = os.environ.get("OTEL_ENVIRONMENT") or os.environ.get(
        "DEPLOYMENT_ENVIRONMENT", "local"
    )
    tag = os.environ.get("OTEL_DEMO_RESOURCE_TAG", "").strip()
    host = socket.gethostname()
    sn = (os.environ.get("OTEL_SERVICE_NAME") or "").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_WORKER_ID") or "").strip() or "unset"
    attrs: dict = {
        "service.name": sn,
        "service.version": "1.0.0",
        "service.instance.id": instance_id,
        "deployment.environment": env,
        "host.name": host,
        "telemetry.sdk.language": "python",
        "telemetry.sdk.name": "opentelemetry",
        "demo.worker_id": cid,
    }
    tier = (os.environ.get("DEMO_PYTHON_TIER") or "").strip()
    if tier:
        attrs["demo.python.tier"] = tier
    if tag:
        attrs["demo.instance.tag"] = tag
    return Resource.create(attrs)


def init_pyroscope_push() -> None:
    """LAB: skopiuj ``init_pyroscope_push`` z ``../python_worker/_src/telemetry.py`` (pyroscope-io)."""
    return


def process_metric_point_attributes() -> dict[str, str]:
    sn = (os.environ.get("OTEL_SERVICE_NAME") or "").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_WORKER_ID") or "").strip() or "unset"
    return {"service.name": sn, "demo.worker_id": cid}


def init_telemetry() -> TracerProvider:
    """
    Minimalna konfiguracja: W3C tracecontext + spany na stderr + metryki tylko w pamięci.

    LAB: zamień na pełny łańcuch OTLP/HTTP (trace + metrics + logi) jak w ``../python_worker/_src/telemetry.py``.
    """
    global _TRACER, _METER
    set_global_textmap(TraceContextTextMapPropagator())
    resource = _build_resource()

    # --- LAB: trace — dodaj OTLPSpanExporter + endpoint z OTEL_EXPORTER_OTLP_TRACES_ENDPOINT ---
    provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
    trace.set_tracer_provider(provider)

    mp = MeterProvider(
        resource=resource,
        metric_readers=[InMemoryMetricReader()],
    )
    metrics.set_meter_provider(mp)

    scope = (os.environ.get("OTEL_SERVICE_NAME") or "gateway_python").strip() or "gateway_python"
    _TRACER = trace.get_tracer(scope, "1.0.0")
    _METER = metrics.get_meter(scope, "1.0.0")

    # --- LAB: observable gauges demo.process.* (psutil) — zob. _register_process_metrics w referencji ---

    return provider


def get_tracer() -> trace.Tracer:
    if _TRACER is None:
        raise RuntimeError("telemetry not initialized")
    return _TRACER


def get_meter() -> metrics.Meter:
    if _METER is None:
        raise RuntimeError("telemetry not initialized")
    return _METER


def is_telemetry_ready() -> bool:
    return _TRACER is not None and _METER is not None
