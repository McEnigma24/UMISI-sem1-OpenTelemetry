//! HTTP pipeline (Rust) — W3C propagate, forward, opcjonalna trasa `route` w JSON.
//! Segment: niepusta `processing_steps`: `[{ "activity", "time" }, …]` lub krok z `nested_route` …
//! Continuous profiling: ustaw ``PYROSCOPE_SERVER`` (np. ``http://pyroscope:4040``) — osobno od OTLP.
use std::collections::HashMap;
use std::env;
use std::net::SocketAddr;
use std::sync::OnceLock;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::post;
use axum::Router;
use opentelemetry::global::{self};
use opentelemetry::logs::{AnyValue, LogRecord, Logger, LoggerProvider, Severity};
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::trace::FutureExt;
use opentelemetry::trace::Span;
use opentelemetry::trace::SpanKind;
use opentelemetry::trace::Status;
use opentelemetry::trace::TraceContextExt;
use opentelemetry::trace::Tracer;
use opentelemetry::Context;
use opentelemetry::InstrumentationScope;
use opentelemetry::KeyValue;
use opentelemetry_http::HeaderExtractor;
use opentelemetry_http::HeaderInjector;
use opentelemetry_otlp::LogExporter;
use opentelemetry_otlp::MetricExporter;
use opentelemetry_otlp::Protocol;
use opentelemetry_otlp::SpanExporter;
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::logs::SdkLoggerProvider;
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::SdkTracerProvider;
use opentelemetry_sdk::Resource;
use serde::Deserialize;
use serde::Serialize;
use serde_json::{json, Value};
use sysinfo::{Pid, ProcessesToUpdate, System};
use tokio::net::TcpListener;
use uuid::Uuid;

/// W OTel Rust 0.31 usunięto `global::logger` / `global::set_logger_provider` — jedna instancja na proces.
static OTLP_LOG_PROVIDER: OnceLock<SdkLoggerProvider> = OnceLock::new();

#[derive(Deserialize, Serialize, Clone, Debug, Default)]
struct ProcessingStep {
    #[serde(default)]
    activity: String,
    #[serde(default)]
    time: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    nested_route: Option<Vec<RouteSeg>>,
}

#[derive(Deserialize, Serialize, Clone, Debug, Default)]
struct RouteSeg {
    id: String,
    #[serde(default)]
    visited: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    processing_time: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    processing_steps: Option<Vec<ProcessingStep>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    http_error_probability: Option<Value>,
}

#[derive(Deserialize, Serialize, Clone, Debug)]
#[serde(default)]
struct PipelineMsg {
    route: Vec<RouteSeg>,
    visit_log: Vec<String>,
    counter: String,
    #[serde(rename = "table_of_workers")]
    table_of_workers: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    http_error_probability: Option<Value>,
}

impl Default for PipelineMsg {
    fn default() -> Self {
        Self {
            route: vec![],
            visit_log: vec![],
            counter: "0".to_string(),
            table_of_workers: vec![],
            http_error_probability: None,
        }
    }
}

struct St {
    id: String,
    path: String,
    peers: HashMap<String, String>,
    max_proc: f64,
    hop_hist: Histogram<f64>,
    msg_counter: Counter<u64>,
}

/// Nagrywa czas hopu (ms) przy każdym wyjściu z obsługi trasy — jak `finally` w Pythonie.
struct HopDurationGuard {
    t0: Instant,
    hop_hist: Histogram<f64>,
    worker_id: String,
}

impl HopDurationGuard {
    fn new(hop_hist: Histogram<f64>, worker_id: String) -> Self {
        Self {
            t0: Instant::now(),
            hop_hist,
            worker_id,
        }
    }
}

impl Drop for HopDurationGuard {
    fn drop(&mut self) {
        let ms = self.t0.elapsed().as_secs_f64() * 1000.0;
        self.hop_hist
            .record(ms, &pipeline_point_kv(self.worker_id.as_str()));
    }
}

fn rs_line(s: &str) {
    let ts = chrono::Local::now().format("%H:%M:%S%.3f");
    println!("{ts} {s}");
    if use_otlp_log_export() {
        if let Some(lp) = OTLP_LOG_PROVIDER.get() {
            let logger = lp.logger("demo.pipeline");
            let mut rec = logger.create_log_record();
            rec.set_body(AnyValue::from(format!("{ts} {s}")));
            rec.set_severity_number(Severity::Info);
            logger.emit(rec);
        }
    }
}

fn use_otlp() -> bool {
    match env::var("OTEL_DEMO_TRACE_EXPORT")
        .unwrap_or_default()
        .as_str()
    {
        "" | "otlp" | "http" => true,
        "ostream" => false,
        _ => true,
    }
}

fn resource() -> Resource {
    let iid = env::var("OTEL_SERVICE_INSTANCE_ID").unwrap_or_else(|_| Uuid::new_v4().to_string());
    let d = env::var("OTEL_ENVIRONMENT")
        .or_else(|_| env::var("DEPLOYMENT_ENVIRONMENT"))
        .unwrap_or_else(|_| "local".to_string());
    let h = env::var("HOSTNAME").unwrap_or_else(|_| "unknown".to_string());
    let mut b = Resource::builder_empty()
        .with_service_name("worker_rust")
        .with_attribute(KeyValue::new("service.version", "1.0.0"))
        .with_attribute(KeyValue::new("service.instance.id", iid))
        .with_attribute(KeyValue::new("deployment.environment", d))
        .with_attribute(KeyValue::new("host.name", h))
        .with_attribute(KeyValue::new("telemetry.sdk.language", "rust"))
        .with_attribute(KeyValue::new("telemetry.sdk.name", "opentelemetry"));
    if let Ok(t) = env::var("OTEL_DEMO_RESOURCE_TAG") {
        let t = t.trim();
        if !t.is_empty() {
            b = b.with_attribute(KeyValue::new("demo.instance.tag", t.to_string()));
        }
    }
    b.build()
}

fn metrics_otlp_endpoint() -> String {
    if let Ok(e) = env::var("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") {
        let t = e.trim();
        if !t.is_empty() {
            return t.to_string();
        }
    }
    let traces = env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        .unwrap_or_else(|_| "http://127.0.0.1:4318/v1/traces".to_string());
    traces.replace("/v1/traces", "/v1/metrics")
}

fn otlp_logs_endpoint() -> String {
    if let Ok(e) = env::var("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT") {
        let t = e.trim();
        if !t.is_empty() {
            return t.to_string();
        }
    }
    let traces = env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        .unwrap_or_else(|_| "http://127.0.0.1:4318/v1/traces".to_string());
    traces.replace("/v1/traces", "/v1/logs")
}

fn use_otlp_log_export() -> bool {
    if !use_otlp() {
        return false;
    }
    let v = env::var("OTEL_DEMO_LOG_EXPORT").unwrap_or_else(|_| "otlp".to_string());
    !matches!(v.to_lowercase().as_str(), "0" | "false" | "no" | "off")
}

fn init_otlp_logs() {
    if !use_otlp() || !use_otlp_log_export() {
        return;
    }
    let ep = otlp_logs_endpoint();
    let exporter = match LogExporter::builder()
        .with_http()
        .with_protocol(Protocol::HttpBinary)
        .with_endpoint(ep.as_str())
        .build()
    {
        Ok(e) => e,
        Err(e) => {
            eprintln!("otlp log exporter: {e}");
            return;
        }
    };
    let lp = SdkLoggerProvider::builder()
        .with_resource(resource())
        .with_batch_exporter(exporter)
        .build();
    if OTLP_LOG_PROVIDER.set(lp).is_err() {
        eprintln!("otlp log provider: already initialized");
    }
}

fn process_metrics_enabled() -> bool {
    let v = env::var("DEMO_PROCESS_METRICS")
        .unwrap_or_default()
        .to_lowercase();
    !matches!(v.as_str(), "0" | "false" | "no" | "off")
}

fn process_metric_point_attributes() -> [KeyValue; 2] {
    let service_name = env::var("OTEL_SERVICE_NAME")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "worker_rust".to_string());
    let worker_id = env::var("DEMO_WORKER_ID")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "rs".to_string());
    [
        KeyValue::new("service.name", service_name),
        KeyValue::new("demo.worker_id", worker_id),
    ]
}

/// Atrybuty punktu metryki pipeline (jak Python): `service.name` + `demo.worker_id` + `worker_id`.
fn pipeline_point_kv(worker_id: &str) -> Vec<KeyValue> {
    let a = process_metric_point_attributes();
    vec![
        a[0].clone(),
        a[1].clone(),
        KeyValue::new("worker_id", worker_id.to_string()),
    ]
}

fn register_process_metrics(meter: &opentelemetry::metrics::Meter) {
    if !process_metrics_enabled() || !use_otlp() {
        return;
    }
    let sys = Arc::new(Mutex::new(System::new_all()));
    let s_cpu = sys.clone();
    let _cpu_g = meter
        .f64_observable_gauge("demo.process.cpu.utilization")
        .with_unit("%")
        .with_description("Użycie CPU procesu 0–100")
        .with_callback(move |o| {
            let mut s = s_cpu.lock().unwrap();
            s.refresh_processes(ProcessesToUpdate::All);
            let pid = Pid::from_u32(std::process::id() as u32);
            if let Some(p) = s.process(pid) {
                let v = (p.cpu_usage() as f64).clamp(0.0, 100.0);
                let attrs = process_metric_point_attributes();
                o.observe(v, &attrs);
            }
        })
        .build();
    let s_mem = sys.clone();
    let _mem_g = meter
        .u64_observable_gauge("demo.process.memory.usage")
        .with_unit("By")
        .with_description("RSS procesu (bajty)")
        .with_callback(move |o| {
            let mut s = s_mem.lock().unwrap();
            s.refresh_processes(ProcessesToUpdate::All);
            let pid = Pid::from_u32(std::process::id() as u32);
            if let Some(p) = s.process(pid) {
                let attrs = process_metric_point_attributes();
                o.observe(p.memory(), &attrs);
            }
        })
        .build();
    let _ = (_cpu_g, _mem_g);
}

async fn spin_cpu_seconds(sec: f64) {
    if sec <= 0.0 {
        return;
    }
    let cx = Context::current();
    let res = tokio::task::spawn_blocking(move || {
        let _guard = cx.clone().attach();
        let deadline = Instant::now() + Duration::from_secs_f64(sec);
        let mut v: u64 = 1;
        while Instant::now() < deadline {
            for _ in 0..2048 {
                v = v.wrapping_mul(1103515245).wrapping_add(12345);
                std::hint::black_box(v);
            }
        }
    })
    .await;
    if let Err(e) = res {
        rs_line(&format!("cpu spin join error: {e}"));
    }
}

fn init_metrics_provider() -> SdkMeterProvider {
    let e = metrics_otlp_endpoint();
    let ex = MetricExporter::builder()
        .with_http()
        .with_protocol(Protocol::HttpBinary)
        .with_endpoint(e)
        .with_timeout(Duration::from_secs(5))
        .build()
        .expect("otlp metrics exporter");
    SdkMeterProvider::builder()
        .with_resource(resource())
        .with_periodic_exporter(ex)
        .build()
}

fn init_otel() -> SdkTracerProvider {
    if use_otlp() {
        let e = env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            .unwrap_or_else(|_| "http://127.0.0.1:4318/v1/traces".to_string());
        let ex = SpanExporter::builder()
            .with_http()
            .with_protocol(Protocol::HttpBinary)
            .with_endpoint(e)
            .with_timeout(Duration::from_secs(5))
            .build()
            .expect("otlp");
        opentelemetry_sdk::trace::SdkTracerProvider::builder()
            .with_resource(resource())
            .with_simple_exporter(ex)
            .build()
    } else {
        let ex = opentelemetry_stdout::SpanExporter::default();
        opentelemetry_sdk::trace::SdkTracerProvider::builder()
            .with_resource(resource())
            .with_simple_exporter(ex)
            .build()
    }
}

fn peer_id_from_env_tail(tail: &str) -> Result<String, String> {
    let t = tail.trim().to_lowercase();
    if t == "py" {
        return Err(
            "DEMO_PEER_PY removed — use DEMO_PEER_PY_GATEWAY / DEMO_PEER_PY_WORKER (route ids py-gateway / py-worker)"
                .into(),
        );
    }
    if t.starts_with("py_") {
        return Ok(t.replace('_', "-"));
    }
    Ok(t)
}

fn load_peer_map() -> Result<HashMap<String, String>, String> {
    if let Ok(raw) = env::var("DEMO_PEER_MAP") {
        let t = raw.trim();
        if !t.is_empty() {
            let v: HashMap<String, String> = serde_json::from_str(t).map_err(|e| e.to_string())?;
            let mut out = HashMap::new();
            for (k, val) in v.into_iter() {
                let kk = peer_id_from_env_tail(&k.to_lowercase())?;
                let vv = val.trim().to_string();
                if !kk.is_empty() && !vv.is_empty() {
                    out.insert(kk, vv);
                }
            }
            return Ok(out);
        }
    }
    let mut m = HashMap::new();
    for (k, v) in env::vars() {
        let rest = k.strip_prefix("DEMO_PEER_");
        if k == "DEMO_PEER_MAP" {
            continue;
        }
        if let Some(tail) = rest {
            let key = peer_id_from_env_tail(&tail.trim().to_lowercase())?;
            let v = v.trim();
            if !key.is_empty() && !v.is_empty() {
                m.insert(key, v.to_string());
            }
        }
    }
    Ok(m)
}

fn max_processing_sec() -> f64 {
    env::var("DEMO_MAX_PROCESSING_SEC")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(120.0)
}

fn parse_processing_time(s: &Option<String>, cap: f64) -> Result<f64, String> {
    let s = match s {
        None => return Ok(0.0),
        Some(x) => x.trim(),
    };
    if s.is_empty() {
        return Ok(0.0);
    }
    let low = s.to_lowercase();
    let sec = if let Some(n) = low.strip_suffix("ms") {
        n.trim()
            .parse::<f64>()
            .map_err(|_| format!("invalid processing_time: {s}"))?
            / 1000.0
    } else if let Some(n) = low.strip_suffix('s') {
        n.trim()
            .parse::<f64>()
            .map_err(|_| format!("invalid processing_time: {s}"))?
    } else {
        return Err(format!(
            "invalid processing_time: {s} (use e.g. 5.6s or 100ms)"
        ));
    };
    if sec < 0.0 {
        return Err("processing_time must be non-negative".to_string());
    }
    Ok(sec.min(cap))
}

fn parse_http_error_probability(raw: Option<&Value>) -> Result<f64, String> {
    let Some(v) = raw else {
        return Ok(0.0);
    };
    let p = if v.is_null() {
        0.0
    } else if let Some(n) = v.as_f64() {
        n
    } else if let Some(s) = v.as_str() {
        let t = s.trim();
        if t.is_empty() {
            0.0
        } else {
            t.parse::<f64>().map_err(|_| {
                "http_error_probability must be a number between 0.0 and 1.0".to_string()
            })?
        }
    } else {
        return Err("http_error_probability must be a number between 0.0 and 1.0".to_string());
    };
    if !(0.0..=1.0).contains(&p) {
        return Err("http_error_probability must be between 0.0 and 1.0".to_string());
    }
    Ok(p)
}

fn effective_http_error_probability(
    global: Option<&Value>,
    seg_local: Option<&Value>,
) -> Result<f64, String> {
    if seg_local.is_some() {
        return parse_http_error_probability(seg_local);
    }
    parse_http_error_probability(global)
}

enum ParsedStep {
    Cpu {
        activity: String,
        sec: f64,
        index: usize,
    },
    Nested {
        activity: String,
        pre_sec: f64,
        route: Vec<RouteSeg>,
        index: usize,
    },
}

fn parse_processing_steps(seg: &RouteSeg, cap: f64) -> Result<Option<Vec<ParsedStep>>, String> {
    let steps = match &seg.processing_steps {
        Some(s) if !s.is_empty() => s,
        _ => return Ok(None),
    };
    let mut v = Vec::new();
    for (i, st) in steps.iter().enumerate() {
        if let Some(nr) = &st.nested_route {
            if nr.is_empty() {
                return Err(format!(
                    "processing_steps[{i}].nested_route must be non-empty"
                ));
            }
            let fid = nr[0].id.trim().to_lowercase();
            if fid == "py" || fid == "py-gateway" {
                return Err(format!(
                    "processing_steps[{i}].nested_route[0].id must not be {fid:?} (workers only)"
                ));
            }
            for (j, sub) in nr.iter().enumerate() {
                route_segment_schema(sub)
                    .map_err(|e| format!("processing_steps[{i}].nested_route[{j}]: {e}"))?;
            }
            let pre_sec = parse_processing_time(&Some(st.time.clone()), cap)?;
            let act = {
                let t = st.activity.trim();
                if t.is_empty() {
                    format!("step_{i}")
                } else {
                    st.activity.clone()
                }
            };
            v.push(ParsedStep::Nested {
                activity: act,
                pre_sec,
                route: nr.clone(),
                index: i,
            });
        } else {
            let sec = parse_processing_time(&Some(st.time.clone()), cap)?;
            let act = {
                let t = st.activity.trim();
                if t.is_empty() {
                    format!("step_{i}")
                } else {
                    st.activity.clone()
                }
            };
            v.push(ParsedStep::Cpu {
                activity: act,
                sec,
                index: i,
            });
        }
    }
    Ok(Some(v))
}

fn route_segment_schema(seg: &RouteSeg) -> Result<(), String> {
    if seg.processing_time.is_some() {
        return Err(
            "field 'processing_time' is not supported; use non-empty 'processing_steps' with {\"activity\",\"time\"}"
                .to_string(),
        );
    }
    match &seg.processing_steps {
        Some(s) if !s.is_empty() => Ok(()),
        _ => Err("non-empty 'processing_steps' is required".to_string()),
    }
}

fn span_name_for_activity(act: &str) -> String {
    let t = act.trim();
    if t.is_empty() {
        return "pipeline.processing_step".to_string();
    }
    t.chars().take(256).collect()
}

fn work_plan_for_segment(seg: &RouteSeg, cap: f64) -> Result<Vec<ParsedStep>, String> {
    route_segment_schema(seg)?;
    parse_processing_steps(seg, cap)?
        .ok_or_else(|| "non-empty 'processing_steps' is required".to_string())
}

fn first_unvisited(route: &[RouteSeg]) -> Option<usize> {
    route.iter().position(|r| !r.visited)
}

fn bump(m: &mut PipelineMsg, cid: &str) {
    let c: i32 = m.counter.parse().unwrap_or(0) + 1;
    m.counter = c.to_string();
    m.table_of_workers.push(cid.to_string());
}

fn peer_url<'a>(peers: &'a HashMap<String, String>, id: &str) -> Option<&'a String> {
    let k = id.trim();
    peers.get(&k.to_lowercase()).or_else(|| peers.get(k))
}

fn nested_pipeline_msg(route: &[RouteSeg], http_error_probability: Option<Value>) -> PipelineMsg {
    let mut r: Vec<RouteSeg> = route.to_vec();
    for s in &mut r {
        s.visited = false;
    }
    PipelineMsg {
        route: r,
        visit_log: vec![],
        counter: "0".to_string(),
        table_of_workers: vec![],
        http_error_probability,
    }
}

async fn post_nested_subpipeline<T>(
    st: &St,
    nested_route: &[RouteSeg],
    http_error_probability: Option<Value>,
    t: &T,
    step_cx: &Context,
) -> Result<(), String>
where
    T: Tracer,
    <T as Tracer>::Span: Send + Sync + 'static,
{
    let first_id = nested_route[0].id.trim();
    let url = peer_url(&st.peers, first_id)
        .ok_or_else(|| format!("no peer URL for nested id {first_id:?}"))?
        .clone();
    let body_vec = serde_json::to_vec(&nested_pipeline_msg(nested_route, http_error_probability))
        .map_err(|e| e.to_string())?;
    rs_line(&format!(
        "[{}] nested_forward to {url}: {}",
        st.id,
        String::from_utf8_lossy(&body_vec)
    ));
    let forward = t
        .span_builder("pipeline.nested_forward")
        .with_kind(SpanKind::Client)
        .start_with_context(t, step_cx);
    let forward_cx = step_cx.clone().with_span(forward);
    let mut hmap = http::HeaderMap::new();
    global::get_text_map_propagator(|p| {
        p.inject_context(&forward_cx, &mut HeaderInjector(&mut hmap))
    });
    let cl = reqwest::Client::new();
    let mut rb = cl.post(&url).body(body_vec);
    for (k, v) in hmap.iter() {
        rb = rb.header(k, v);
    }
    rb = rb.header("content-type", "application/json");
    let resp_fut = async {
        let resp = rb.send().await.map_err(|e| e.to_string())?;
        let c = resp.status();
        let _txt = resp.text().await.map_err(|e| e.to_string())?;
        Ok::<http::StatusCode, String>(
            http::StatusCode::from_u16(c.as_u16()).unwrap_or(http::StatusCode::BAD_GATEWAY),
        )
    }
    .with_context(forward_cx);
    let status = match resp_fut.await {
        Ok(x) => x,
        Err(e) => return Err(e),
    };
    if !status.is_success() {
        return Err(format!("nested_forward HTTP {status}"));
    }
    Ok(())
}

async fn pipeline(
    State(st): State<Arc<St>>,
    headers: HeaderMap,
    body: Bytes,
) -> axum::response::Response {
    let parent: Context =
        global::get_text_map_propagator(|p| p.extract(&HeaderExtractor(&headers)));

    let m: PipelineMsg = match serde_json::from_slice(&body) {
        Ok(x) => x,
        Err(e) => {
            return (StatusCode::BAD_REQUEST, e.to_string()).into_response();
        }
    };
    rs_line(&format!(
        "[{}] received: {}",
        st.id,
        String::from_utf8_lossy(&body)
    ));

    route_mode(&st, &parent, m).await
}

async fn route_mode(st: &St, parent: &Context, mut m: PipelineMsg) -> Response {
    use axum::body::Body;

    let _hop_duration = HopDurationGuard::new(st.hop_hist.clone(), st.id.clone());

    if m.route.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            "JSON must include non-empty route array",
        )
            .into_response();
    }

    let t = global::tracer_with_scope(
        InstrumentationScope::builder("worker_rust")
            .with_version("1.0.0")
            .build(),
    );

    let idx = match first_unvisited(&m.route) {
        Some(i) => i,
        None => {
            return (StatusCode::BAD_REQUEST, "route has no unvisited segment").into_response();
        }
    };
    let seg_id = m.route[idx].id.clone();
    if seg_id != st.id {
        return (
            StatusCode::BAD_REQUEST,
            format!(
                "first unvisited route segment id must match this node (expected {}, got {})",
                st.id, seg_id
            ),
        )
            .into_response();
    }
    let http_error_probability = match effective_http_error_probability(
        m.http_error_probability.as_ref(),
        m.route[idx].http_error_probability.as_ref(),
    ) {
        Ok(p) => p,
        Err(e) => return (StatusCode::BAD_REQUEST, e).into_response(),
    };

    let mut hop = t
        .span_builder("pipeline.hop")
        .with_kind(SpanKind::Server)
        .start_with_context(&t, parent);
    hop.set_attribute(KeyValue::new("demo.worker_id", st.id.clone()));
    if http_error_probability > 0.0 && rand::random::<f64>() < http_error_probability {
        hop.set_attribute(KeyValue::new("demo.simulated_http_error", true));
        hop.set_attribute(KeyValue::new(
            "demo.http_error_probability",
            http_error_probability,
        ));
        hop.set_status(Status::error("simulated_http_error"));
        let body = json!({
            "error": "simulated_http_error",
            "worker_id": st.id,
            "probability": http_error_probability
        })
        .to_string();
        return Response::builder()
            .status(StatusCode::INTERNAL_SERVER_ERROR)
            .header("content-type", "application/json")
            .body(Body::from(body))
            .unwrap_or_else(|e| {
                (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
            });
    }
    let hop_cx = parent.clone().with_span(hop);

    let steps = match work_plan_for_segment(&m.route[idx], st.max_proc) {
        Ok(p) => p,
        Err(e) => return (StatusCode::BAD_REQUEST, e).into_response(),
    };

    {
        let cpu_sum: f64 = steps
            .iter()
            .map(|s| match s {
                ParsedStep::Cpu { sec, .. } => *sec,
                ParsedStep::Nested { pre_sec, .. } => *pre_sec,
            })
            .sum();
        let has_nested = steps.iter().any(|s| matches!(s, ParsedStep::Nested { .. }));
        let mut proc = t.start_with_context("pipeline.processing", &hop_cx);
        proc.set_attribute(KeyValue::new("demo.processing.mode", "steps"));
        proc.set_attribute(KeyValue::new("demo.step_count", steps.len() as i64));
        proc.set_attribute(KeyValue::new("demo.simulated_processing_sec", cpu_sum));
        proc.set_attribute(KeyValue::new("demo.has_nested_steps", has_nested));
        let proc_cx = hop_cx.clone().with_span(proc);
        for step in steps.iter() {
            match step {
                ParsedStep::Cpu {
                    activity: act,
                    sec,
                    index: i,
                } => {
                    let name = span_name_for_activity(act);
                    let mut step_sp = t.start_with_context(name, &proc_cx);
                    step_sp.set_attribute(KeyValue::new("demo.activity", act.clone()));
                    step_sp.set_attribute(KeyValue::new("demo.cpu_spin_sec", *sec));
                    step_sp.set_attribute(KeyValue::new("demo.step_index", *i as i64));
                    let step_cx = proc_cx.clone().with_span(step_sp);
                    if *sec > 0.0 {
                        async { spin_cpu_seconds(*sec).await }
                            .with_context(step_cx.clone())
                            .await;
                    }
                }
                ParsedStep::Nested {
                    activity: act,
                    pre_sec,
                    route,
                    index: i,
                } => {
                    let name = span_name_for_activity(act);
                    let mut step_sp = t.start_with_context(name, &proc_cx);
                    step_sp.set_attribute(KeyValue::new("demo.activity", act.clone()));
                    step_sp.set_attribute(KeyValue::new("demo.cpu_spin_sec", *pre_sec));
                    step_sp.set_attribute(KeyValue::new("demo.step_index", *i as i64));
                    step_sp.set_attribute(KeyValue::new("demo.nested_subpipeline", true));
                    let step_cx = proc_cx.clone().with_span(step_sp);
                    if *pre_sec > 0.0 {
                        async { spin_cpu_seconds(*pre_sec).await }
                            .with_context(step_cx.clone())
                            .await;
                    }
                    if let Err(e) = post_nested_subpipeline(
                        st,
                        route.as_slice(),
                        m.http_error_probability.clone(),
                        &t,
                        &step_cx,
                    )
                    .await
                    {
                        return (StatusCode::BAD_GATEWAY, e).into_response();
                    }
                }
            }
        }

        m.route[idx].visited = true;
        m.visit_log.push(st.id.clone());
        bump(&mut m, &st.id);

        let next_idx = first_unvisited(&m.route);
        let body_vec = match serde_json::to_vec(&m) {
            Ok(b) => b,
            Err(e) => {
                return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
            }
        };

        if let Some(ni) = next_idx {
            let next_id = m.route[ni].id.clone();
            let url = match peer_url(&st.peers, &next_id) {
                Some(u) => u.clone(),
                None => {
                    return (
                        StatusCode::BAD_GATEWAY,
                        format!("no peer URL for id {next_id:?}"),
                    )
                        .into_response();
                }
            };
            rs_line(&format!(
                "[{}] forward to {url}: {}",
                st.id,
                String::from_utf8_lossy(&body_vec)
            ));
            let forward = t
                .span_builder("pipeline.forward")
                .with_kind(SpanKind::Client)
                .start_with_context(&t, &proc_cx);
            let forward_cx = proc_cx.clone().with_span(forward);
            let mut hmap = http::HeaderMap::new();
            global::get_text_map_propagator(|p| {
                p.inject_context(&forward_cx, &mut HeaderInjector(&mut hmap))
            });
            let cl = reqwest::Client::new();
            let mut rb = cl.post(&url).body(body_vec);
            for (k, v) in hmap.iter() {
                rb = rb.header(k, v);
            }
            rb = rb.header("content-type", "application/json");
            let resp_fut = async {
                let resp = rb.send().await.map_err(|e| e.to_string())?;
                let c = resp.status();
                let txt = resp.text().await.map_err(|e| e.to_string())?;
                Ok::<(http::StatusCode, String), String>((
                    http::StatusCode::from_u16(c.as_u16()).unwrap_or(http::StatusCode::BAD_GATEWAY),
                    txt,
                ))
            }
            .with_context(forward_cx);
            let (status, out) = match resp_fut.await {
                Ok(x) => x,
                Err(e) => {
                    return (StatusCode::BAD_GATEWAY, e).into_response();
                }
            };
            st.msg_counter.add(1, &pipeline_point_kv(st.id.as_str()));
            return Response::builder()
                .status(status)
                .header("content-type", "application/json")
                .body(Body::from(out))
                .unwrap_or_else(|e| {
                    (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
                });
        }

        let out = match serde_json::to_string(&m) {
            Ok(x) => x,
            Err(e) => {
                return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
            }
        };
        rs_line(&format!("[{}] respond (terminal route): {out}", st.id));
        st.msg_counter.add(1, &pipeline_point_kv(st.id.as_str()));
        Response::builder()
            .status(StatusCode::OK)
            .header("content-type", "application/json")
            .body(Body::from(out))
            .unwrap_or_else(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response())
    }
}

/// Continuous profiling → Pyroscope (osobno od OTLP metryk). ``PYROSCOPE_SERVER`` ustawiony i niepusty.
fn maybe_start_pyroscope_push() {
    use std::thread;

    let en = env::var("PYROSCOPE_ENABLED").unwrap_or_else(|_| "true".to_string());
    let el = en.to_lowercase();
    if matches!(el.as_str(), "0" | "false" | "no" | "off") {
        return;
    }
    let server = match env::var("PYROSCOPE_SERVER") {
        Ok(s) if !s.trim().is_empty() => s,
        _ => return,
    };
    let app_name = env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| "worker_rust".to_string());
    let worker_id = env::var("DEMO_WORKER_ID").unwrap_or_else(|_| "rs".to_string());
    thread::spawn(move || {
        use pyroscope::backend::{pprof_backend, BackendConfig, PprofConfig};
        use pyroscope::pyroscope::PyroscopeAgentBuilder;

        let backend = pprof_backend(PprofConfig::default(), BackendConfig::default());
        let agent = match PyroscopeAgentBuilder::new(
            server.as_str(),
            app_name.as_str(),
            100u32,
            "pyroscope-rs",
            env!("CARGO_PKG_VERSION"),
            backend,
        )
        .tags(vec![
            ("service.name", app_name.as_str()),
            ("demo.worker_id", worker_id.as_str()),
        ])
        .build()
        {
            Ok(a) => a,
            Err(e) => {
                eprintln!("pyroscope build: {e}");
                return;
            }
        };
        let _pyroscope_guard = match agent.start() {
            Ok(r) => r,
            Err(e) => {
                eprintln!("pyroscope start: {e}");
                return;
            }
        };
        eprintln!("Pyroscope push profiler: server='{server}' application_name='{app_name}'");
        thread::park();
    });
}

#[tokio::main]
async fn main() {
    if env::var("DEMO_MODE")
        .unwrap_or_else(|_| "pipeline".to_string())
        .eq_ignore_ascii_case("exercises")
    {
        rs_line("DEMO_MODE=exercises — użyj DEMO_MODE=pipeline.");
        return;
    }
    // Bez tego `extract`/`inject` używają propagatora „noop” — każdy hop ma nowy trace_id
    // zamiast kontynuacji łańcucha z gatewaya (W3C traceparent).
    global::set_text_map_propagator(TraceContextPropagator::new());
    let prov = init_otel();
    let keep = prov.clone();
    global::set_tracer_provider(prov);
    let _meter_prov = if use_otlp() {
        let mp = init_metrics_provider();
        global::set_meter_provider(mp.clone());
        Some(mp)
    } else {
        None
    };
    init_otlp_logs();
    let meter = global::meter_with_scope(
        InstrumentationScope::builder("worker_rust")
            .with_version("1.0.0")
            .build(),
    );
    let hop_hist = meter
        .f64_histogram("demo.pipeline.hop.duration_ms")
        .with_unit("ms")
        .with_description("Czas przetworzenia i ewent. forward jednego hopy")
        .build();
    let msg_counter = meter
        .u64_counter("demo.pipeline.messages")
        .with_description("Liczba przetworzonych wiadomości w węźle")
        .build();
    register_process_metrics(&meter);
    maybe_start_pyroscope_push();
    let peers = match load_peer_map() {
        Ok(m) => m,
        Err(e) => {
            eprintln!("peer map config error: {e}");
            return;
        }
    };
    let st = Arc::new(St {
        id: env::var("DEMO_WORKER_ID").unwrap_or_else(|_| "rs".to_string()),
        path: env::var("DEMO_HTTP_PATH").unwrap_or_else(|_| "/v1/pipeline".to_string()),
        peers,
        max_proc: max_processing_sec(),
        hop_hist,
        msg_counter,
    });
    let a: SocketAddr = env::var("DEMO_HTTP_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:8080".to_string())
        .parse()
        .expect("DEMO_HTTP_ADDR");
    let app = Router::new()
        .route(st.path.as_str(), post(pipeline))
        .with_state(st.clone());
    let l = TcpListener::bind(a).await.expect("bind");
    rs_line(&format!(
        "Rust pipeline http://{a}{} peers={:?}",
        st.path,
        st.peers.keys().collect::<Vec<_>>()
    ));
    axum::serve(l, app).await.expect("serve");
    let _ = keep.shutdown();
}
