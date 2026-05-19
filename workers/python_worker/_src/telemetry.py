"""
OpenTelemetry + Pyroscope: inicjalizacja eksportu OTLP/HTTP, propagacja W3C, metryki procesu, logi OTLP.

Wywołaj ``init_telemetry()`` przed utworzeniem handlera HTTP; potem ``get_tracer()`` / ``get_meter()``.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import uuid
from typing import Any

import psutil

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Observation
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_TRACER: trace.Tracer | None = None
_METER: metrics.Meter | None = None
_LOG_PROVIDER: LoggerProvider | None = None


def log_pipeline_line(msg: str) -> None:
    """Stdout + opcjonalnie eksport logów OTLP (LoggerProvider z ``init_telemetry``)."""
    from datetime import datetime

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


def init_pyroscope_push() -> None:
    """Continuous profiling → Pyroscope (Grafana Drilldown Profiles). Osobno od OTLP metryk ``demo.process.*``."""
    if not _pyroscope_push_enabled():
        return
    try:
        import pyroscope
    except ImportError:
        log_pipeline_line(
            "PYROSCOPE_SERVER set but pyroscope-io missing — rebuild image (requirements.txt)"
        )
        return
    server = os.environ.get("PYROSCOPE_SERVER", "").strip()
    app_name = (os.environ.get("OTEL_SERVICE_NAME") or "").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_WORKER_ID") or "").strip() or "unset"
    env = os.environ.get("OTEL_ENVIRONMENT") or os.environ.get(
        "DEPLOYMENT_ENVIRONMENT", "local"
    )
    try:
        pyroscope.configure(
            application_name=app_name,
            server_address=server,
            tags={
                "service.name": app_name,
                "demo.worker_id": cid,
                "deployment.environment": env,
            },
        )
    except Exception as e:
        log_pipeline_line(
            f"Pyroscope configure failed (profiling disabled for this run): {type(e).__name__}: {e}"
        )
        return
    log_pipeline_line(
        f"Pyroscope push profiler: server='{server}' application_name='{app_name}'"
    )


def process_metric_point_attributes() -> dict[str, str]:
    """Atrybuty punktu OTLP → osobne serie w Prometheusie (resource często nie trafia jako label)."""
    sn = (os.environ.get("OTEL_SERVICE_NAME") or "").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_WORKER_ID") or "").strip() or "unset"
    return {"service.name": sn, "demo.worker_id": cid}


def _register_process_metrics(meter: metrics.Meter) -> None:
    if not _process_metrics_enabled():
        return
    proc = psutil.Process()

    def _cpu_cb(_options: Any) -> Any:
        pct = float(proc.cpu_percent(interval=None))
        pct = min(100.0, max(0.0, pct))
        yield Observation(pct, process_metric_point_attributes())

    def _mem_cb(_options: Any) -> Any:
        attrs = process_metric_point_attributes()
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


def init_telemetry() -> TracerProvider:
    """Tracer + meter (+ opcjonalnie logi OTLP). Zwraca ``TracerProvider`` do ``force_flush`` przy shutdown."""
    global _TRACER, _METER, _LOG_PROVIDER
    _LOG_PROVIDER = None
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

    scope = (os.environ.get("OTEL_SERVICE_NAME") or "gateway_python").strip() or "gateway_python"
    _TRACER = trace.get_tracer(scope, "1.0.0")
    _METER = metrics.get_meter(scope, "1.0.0")
    if _use_otlp_http() and _process_metrics_enabled():
        _register_process_metrics(_METER)
    return provider  # type: ignore[return-value]


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
