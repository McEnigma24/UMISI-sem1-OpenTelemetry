//! Prosty klient OpenTelemetry (OTLP/HTTP JSON albo stdout) — analog
//! `clients/python/_src/main.py` / `clients/cpp/_src/main.cpp`.
//! Endpoint: `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (domyślnie `http://127.0.0.1:4318/v1/traces`).
//! Tryb: `OTEL_DEMO_TRACE_EXPORT` — puste / `otlp` / `http` → OTLP; `ostream` → stdout (jak ConsoleSpanExporter w Pythonie).

use std::env;
use std::time::{Duration, Instant, SystemTime};

use opentelemetry::global;
use opentelemetry::global::BoxedTracer;
use opentelemetry::trace::{get_active_span, Span, SpanKind, Status, Tracer};
use opentelemetry::Context;
use opentelemetry::InstrumentationScope;
use opentelemetry::KeyValue;
use opentelemetry_otlp::{Protocol, SpanExporter, WithExportConfig};
use opentelemetry_sdk::trace::SdkTracerProvider;
use opentelemetry_sdk::Resource;

fn line(msg: &str) {
    println!("{msg}");
}

fn use_otlp_http() -> bool {
    match env::var("OTEL_DEMO_TRACE_EXPORT")
        .unwrap_or_default()
        .as_str()
    {
        "" => true,
        "ostream" => false,
        "otlp" | "http" => true,
        _ => true,
    }
}

fn demo_resource() -> Resource {
    Resource::builder_empty()
        .with_service_name("demo_app")
        .with_attribute(KeyValue::new("service.version", "1.0.0"))
        .build()
}

fn init_tracer_otlp_http() -> SdkTracerProvider {
    let endpoint = env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        .unwrap_or_else(|_| "http://127.0.0.1:4318/v1/traces".to_string());

    let exporter = SpanExporter::builder()
        .with_http()
        .with_protocol(Protocol::HttpJson)
        .with_endpoint(endpoint)
        .with_timeout(Duration::from_secs(5))
        .build()
        .expect("OTLP HTTP span exporter");

    let provider = SdkTracerProvider::builder()
        .with_resource(demo_resource())
        .with_simple_exporter(exporter)
        .build();

    let keep = provider.clone();
    global::set_tracer_provider(provider);
    keep
}

fn init_tracer_stdout() -> SdkTracerProvider {
    let exporter = opentelemetry_stdout::SpanExporter::default();
    let provider = SdkTracerProvider::builder()
        .with_resource(demo_resource())
        .with_simple_exporter(exporter)
        .build();

    let keep = provider.clone();
    global::set_tracer_provider(provider);
    keep
}

fn otel_api_exercises(tracer: &BoxedTracer) {
    // [1/6] zagnieżdżone spany + aktywny kontekst
    tracer.in_span("Outer operation", |_| {
        tracer.in_span("Inner operation", |_| {
            let inner_valid = get_active_span(|s| s.span_context().is_valid());
            line(&format!(
                "OpenTelemetry [1/6]: nested spans; active span context valid={inner_valid}"
            ));
        });
    });

    // [2/6] atrybuty, zdarzenia, CLIENT
    let mut span = tracer
        .span_builder("enriched")
        .with_kind(SpanKind::Client)
        .with_attributes([
            KeyValue::new("http.method", "GET"),
            KeyValue::new("http.status_code", 200i64),
        ])
        .start(tracer);
    span.set_attribute(KeyValue::new("work.units", 42.0f64));
    span.set_attribute(KeyValue::new("flag.ok", true));
    span.add_event("checkpoint", vec![]);
    span.add_event(
        "with_attrs",
        vec![KeyValue::new("step", "after_io"), KeyValue::new("rc", 0i64)],
    );
    line("OpenTelemetry [2/6]: attributes, events, SpanKind::CLIENT (stdout vs OTLP/HTTP)");
    span.set_status(Status::Ok);
    span.end();

    // [3/6] update_name + błąd
    let mut span = tracer.start("original_name");
    span.update_name("renamed_op");
    span.set_status(Status::error("synthetic failure for status path"));
    span.end();
    line("OpenTelemetry [3/6]: UpdateName, SetStatus(Error)");

    // [4/6] root bez rodzica w kontekście (jak jawny root w Pythonie)
    let mut root_span = tracer.start_with_context("explicit_root", &Context::new());
    line("OpenTelemetry [4/6]: root span (Context::new() = brak parent trace w OTel Rust)");
    root_span.set_status(Status::Ok);
    root_span.end();

    // [5/6] IsRecording
    let mut s = tracer.start("recording_probe");
    let rec = s.is_recording();
    s.set_status(Status::Ok);
    s.end();
    if rec {
        line("OpenTelemetry [5/6]: IsRecording() == true (SDK span)");
    } else {
        line("OpenTelemetry [5/6]: IsRecording() == false (unexpected with SDK — check config)");
    }

    // [6/6] SpanContext
    let mut s = tracer.start("context_probe");
    let valid = s.span_context().is_valid();
    s.set_status(Status::Ok);
    s.end();
    if valid {
        line("OpenTelemetry [6/6]: GetContext().IsValid() == true");
    } else {
        line("OpenTelemetry [6/6]: GetContext().IsValid() == false (unexpected with SDK)");
    }
}

fn main() {
    line(&format!(
        "It just works (Rust client, t={:?})",
        SystemTime::now()
    ));

    let provider = if use_otlp_http() {
        line("OTEL_DEMO_TRACE_EXPORT=otlp: eksport HTTP JSON (endpoint: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)");
        init_tracer_otlp_http()
    } else {
        line("Domyślnie stdout exporter. OTLP: export OTEL_DEMO_TRACE_EXPORT=otlp");
        init_tracer_stdout()
    };

    let scope = InstrumentationScope::builder("demo_app")
        .with_version("1.0.0")
        .build();
    let tracer = global::tracer_with_scope(scope);

    line("Rust client — simple demo (args + sleep)");
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        line("No arguments. Try: cargo run -- hello world");
    } else {
        line(&format!("Arguments: {}", args.join(" ")));
    }

    otel_api_exercises(&tracer);

    let start = Instant::now();
    std::thread::sleep(Duration::from_millis(50));
    line(&format!("Elapsed: {:?}", start.elapsed()));

    line("OpenTelemetry: zakończone (stdout vs OTLP — patrz OTEL_DEMO_TRACE_EXPORT).");
    let _ = provider.force_flush();
    let _ = provider.shutdown();
}
