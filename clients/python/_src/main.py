#!/usr/bin/env python3
"""
Prosty klient OpenTelemetry (OTLP/HTTP, JSON) — odpowiednik clients/cpp/_src/main.cpp.
Przed uruchomieniem: opcjonalnie słuchacz np. clients/cpp/listen_otlp_http.sh 4318
Endpoint: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (domyślnie http://127.0.0.1:4318/v1/traces).
"""
from __future__ import annotations

import os
import sys
import time

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)


def _line(msg: str) -> None:
    print(msg, flush=True)


def _use_otlp_http() -> bool:
    m = os.environ.get("OTEL_DEMO_TRACE_EXPORT", "")
    if not m:
        return True  # jak w main.cpp z otlp = True
    if m == "ostream":
        return False
    if m in ("otlp", "http"):
        return True
    return False


def _init_tracer_otlp_http() -> TracerProvider:
    endpoint = os.environ.get(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"
    )
    # blocking eksport: SimpleSpanProcessor — każdy End() wysyła (jak w C++ SimpleSpanProcessor)
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        timeout=5,  # sekundy (C++: std::chrono::seconds(5))
    )
    resource = Resource.create(
        {
            "service.name": "demo_app",
            "service.version": "1.0.0",
        }
    )
    provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def _init_tracer_stdout() -> TracerProvider:
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    resource = Resource.create(
        {
            "service.name": "demo_app",
            "service.version": "1.0.0",
        }
    )
    provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr))
    )
    trace.set_tracer_provider(provider)
    return provider


def _otel_api_exercises(tr) -> None:
    # ---- 1) zagnieżdżone spany ----
    with tr.start_as_current_span("Outer operation") as _outer:
        with tr.start_as_current_span("Inner operation") as inner:
            current_matches = trace.get_current_span() is inner
            _line("OpenTelemetry [1/6]: nested spans; GetCurrentSpan == inner span")
            if not current_matches:
                _line(
                    "OpenTelemetry [1/6]: note — GetCurrentSpan not equal to inner (check impl.)"
                )

    # ---- 2) atrybuty, zdarzenia, kind CLIENT ----
    with tr.start_as_current_span(
        "enriched",
        kind=trace.SpanKind.CLIENT,
        attributes={
            "http.method": "GET",
            "http.status_code": 200,
        },
    ) as span:
        span.set_attribute("work.units", 42.0)
        span.set_attribute("flag.ok", True)
        span.add_event("checkpoint")
        span.add_event("with_attrs", {"step": "after_io", "rc": 0})
        _line("OpenTelemetry [2/6]: attributes, events, SpanKind::CLIENT (export: stderr albo OTLP/HTTP)")
        span.set_status(StatusCode.OK)

    # ---- 3) update_name + błąd ----
    with tr.start_as_current_span("original_name") as span:
        span.update_name("renamed_op")
        span.set_status(
            Status(StatusCode.ERROR, "synthetic failure for status path"),
        )
        _line("OpenTelemetry [3/6]: UpdateName, SetStatus(Error)")

    # ---- 4) jawny root: kontekst z „pustym” (invalid) spanem, jak w C++ (Context + kIsRootSpanKey) ----
    invalid = SpanContext(0, 0, is_remote=False, trace_flags=TraceFlags(0), trace_state=[])
    root_ctx = set_span_in_context(NonRecordingSpan(invalid))
    root_token = otel_context.attach(root_ctx)
    try:
        with tr.start_as_current_span("explicit_root") as root_span:
            _line("OpenTelemetry [4/6]: root span (NowyContext z invalid = brak parent trace)")
            root_span.set_status(StatusCode.OK)
    finally:
        otel_context.detach(root_token)

    # ---- 5) Is_recording ----
    with tr.start_as_current_span("recording_probe") as s:
        rec = s.is_recording()
        s.set_status(StatusCode.OK)
    if rec:
        _line("OpenTelemetry [5/6]: IsRecording() == true (SDK span)")
    else:
        _line("OpenTelemetry [5/6]: IsRecording() == false (unexpected with SDK — check config)")

    # ---- 6) poprawność SpanContext ----
    with tr.start_as_current_span("context_probe") as s:
        valid = s.get_span_context().is_valid
        s.set_status(StatusCode.OK)
    if valid:
        _line("OpenTelemetry [6/6]: GetContext().IsValid() == true")
    else:
        _line("OpenTelemetry [6/6]: GetContext().IsValid() == false (unexpected with SDK)")


def main() -> int:
    _line(
        f"It just works (t={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())})"
    )
    if _use_otlp_http():
        _line(
            "OTEL_DEMO_TRACE_EXPORT=otlp: eksport HTTP (nc: ./listen_otlp_http.sh ; endpoint: OTEL_EXPORTER_OTLP_*)"
        )
        provider = _init_tracer_otlp_http()
    else:
        _line(
            "Domyślnie ostream/stderr. OTLP/HTTP:  export OTEL_DEMO_TRACE_EXPORT=otlp"
        )
        provider = _init_tracer_stdout()

    tr = trace.get_tracer("demo_app", "1.0.0")
    _otel_api_exercises(tr)

    _line("OpenTelemetry: zakończone (stderr vs OTLP — patrz OTEL_DEMO_TRACE_EXPORT).")
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=10_000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
