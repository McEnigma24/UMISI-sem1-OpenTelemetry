"""
Incomplete telemetry module for the Python worker lab.

The HTTP pipeline imports this module in both complete and incomplete modes. In
this lab version the public API is preserved, but it intentionally does not
export traces, metrics, logs or profiles. Students should replace the no-op
pieces with the complete OpenTelemetry setup from
``workers/python_worker/_src/telemetry.py``.
"""
from __future__ import annotations

import os
from typing import Any


def log_pipeline_line(msg: str) -> None:
    """Stdout-only logging.

    LAB TODO: configure LoggerProvider + OTLPLogExporter and also send this
    message through an OpenTelemetry LoggingHandler.
    """
    from datetime import datetime

    now = datetime.now()
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts} {msg}", flush=True)


def process_metric_point_attributes() -> dict[str, str]:
    """Attributes that future metric points should use.

    LAB TODO: reuse these when recording demo.pipeline.* and demo.process.*
    metrics, so Prometheus/Grafana can group by service and worker.
    """
    sn = (os.environ.get("OTEL_SERVICE_NAME") or "gateway_python").strip() or "gateway_python"
    cid = (os.environ.get("DEMO_WORKER_ID") or "unset").strip() or "unset"
    return {"service.name": sn, "demo.worker_id": cid}


def init_telemetry() -> Any:
    """Initialize telemetry providers.

    LAB TODO: create and register:
    - Resource with service.name, service.instance.id, demo.worker_id, demo.python.tier
    - TracerProvider + OTLPSpanExporter
    - MeterProvider + OTLPMetricExporter
    - LoggerProvider + OTLPLogExporter
    - W3C TraceContext propagator
    """
    return None


def init_pyroscope_push() -> None:
    """Start Pyroscope push profiling.

    LAB TODO: configure pyroscope-io with PYROSCOPE_SERVER and service tags.
    This is separate from OpenTelemetry metrics/traces.
    """
    return


def is_telemetry_ready() -> bool:
    """Incomplete mode is runnable before telemetry is implemented."""
    return True
