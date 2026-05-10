//! HTTP pipeline (Rust) — W3C propagate, forward, opcjonalna trasa `route` w JSON.
use std::collections::HashMap;
use std::env;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::routing::post;
use axum::Router;
use opentelemetry::global;
use opentelemetry::trace::FutureExt;
use opentelemetry::trace::TraceContextExt;
use opentelemetry::trace::Tracer;
use opentelemetry::Context;
use opentelemetry::InstrumentationScope;
use opentelemetry::KeyValue;
use opentelemetry_http::HeaderExtractor;
use opentelemetry_http::HeaderInjector;
use opentelemetry_otlp::Protocol;
use opentelemetry_otlp::SpanExporter;
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::trace::SdkTracerProvider;
use opentelemetry_sdk::Resource;
use serde::Deserialize;
use serde::Serialize;
use tokio::net::TcpListener;
use uuid::Uuid;

#[derive(Deserialize, Serialize, Clone, Debug, Default)]
struct RouteSeg {
    id: String,
    #[serde(default)]
    visited: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    processing_time: Option<String>,
}

#[derive(Deserialize, Serialize, Clone, Debug)]
#[serde(default)]
struct PipelineMsg {
    route: Vec<RouteSeg>,
    visit_log: Vec<String>,
    counter: String,
    #[serde(rename = "table_of_clients")]
    table_of_clients: Vec<String>,
}

impl Default for PipelineMsg {
    fn default() -> Self {
        Self {
            route: vec![],
            visit_log: vec![],
            counter: "0".to_string(),
            table_of_clients: vec![],
        }
    }
}

struct St {
    id: String,
    path: String,
    peers: HashMap<String, String>,
    max_proc: f64,
}

fn rs_line(s: &str) {
    let ts = chrono::Local::now().format("%H:%M:%S%.3f");
    println!("{ts} {s}");
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
        .with_service_name("demo_app")
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

fn init_otel() -> SdkTracerProvider {
    if use_otlp() {
        let e = env::var("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            .unwrap_or_else(|_| "http://127.0.0.1:4318/v1/traces".to_string());
        let ex = SpanExporter::builder()
            .with_http()
            .with_protocol(Protocol::HttpJson)
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

fn load_peer_map() -> Result<HashMap<String, String>, String> {
    if let Ok(raw) = env::var("DEMO_PEER_MAP") {
        let t = raw.trim();
        if !t.is_empty() {
            let v: HashMap<String, String> = serde_json::from_str(t).map_err(|e| e.to_string())?;
            return Ok(v
                .into_iter()
                .map(|(k, v)| (k.to_lowercase(), v.trim().to_string()))
                .filter(|(_, v)| !v.is_empty())
                .collect());
        }
    }
    let mut m = HashMap::new();
    for (k, v) in env::vars() {
        let rest = k.strip_prefix("DEMO_PEER_");
        if k == "DEMO_PEER_MAP" {
            continue;
        }
        if let Some(tail) = rest {
            let tail = tail.trim().to_lowercase();
            let v = v.trim();
            if !tail.is_empty() && !v.is_empty() {
                m.insert(tail, v.to_string());
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
        return Err(format!("invalid processing_time: {s} (use e.g. 5.6s or 100ms)"));
    };
    if sec < 0.0 {
        return Err("processing_time must be non-negative".to_string());
    }
    Ok(sec.min(cap))
}

fn first_unvisited(route: &[RouteSeg]) -> Option<usize> {
    route.iter().position(|r| !r.visited)
}

fn bump(m: &mut PipelineMsg, cid: &str) {
    let c: i32 = m.counter.parse().unwrap_or(0) + 1;
    m.counter = c.to_string();
    m.table_of_clients.push(cid.to_string());
}

fn peer_url<'a>(peers: &'a HashMap<String, String>, id: &str) -> Option<&'a String> {
    let k = id.trim();
    peers
        .get(&k.to_lowercase())
        .or_else(|| peers.get(k))
}

async fn pipeline(
    State(st): State<Arc<St>>,
    headers: HeaderMap,
    body: Bytes,
) -> axum::response::Response {
    let parent: Context =
        global::get_text_map_propagator(|p| p.extract(&HeaderExtractor(&headers)));

    let mut m: PipelineMsg = match serde_json::from_slice(&body) {
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

    if m.route.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            "JSON must include non-empty route array",
        )
            .into_response();
    }

    route_mode(&st, &parent, m).await
}

async fn route_mode(st: &St, parent: &Context, mut m: PipelineMsg) -> Response {
    use axum::body::Body;

    let t = global::tracer_with_scope(
        InstrumentationScope::builder("demo_app")
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
    let dur = match parse_processing_time(&m.route[idx].processing_time, st.max_proc) {
        Ok(d) => d,
        Err(e) => return (StatusCode::BAD_REQUEST, e).into_response(),
    };

    let hop = t.start_with_context("pipeline.hop", parent);
    let hop_cx = parent.clone().with_span(hop);

    if dur > 0.0 {
        let sw = t.start_with_context("pipeline.simulated_work", &hop_cx);
        let sw_cx = hop_cx.clone().with_span(sw);
        let sl = async {
            tokio::time::sleep(Duration::from_secs_f64(dur)).await;
        }
        .with_context(sw_cx);
        sl.await;
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
        let forward = t.start_with_context("pipeline.forward", &hop_cx);
        let forward_cx = hop_cx.clone().with_span(forward);
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
        return Response::builder()
            .status(status)
            .header("content-type", "application/json")
            .body(Body::from(out))
            .unwrap_or_else(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response());
    }

    let out = match serde_json::to_string(&m) {
        Ok(x) => x,
        Err(e) => {
            return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
        }
    };
    rs_line(&format!(
        "[{}] respond (terminal route): {out}",
        st.id
    ));
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "application/json")
        .body(Body::from(out))
        .unwrap_or_else(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response())
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
    let prov = init_otel();
    let keep = prov.clone();
    global::set_tracer_provider(prov);
    let peers = match load_peer_map() {
        Ok(m) => m,
        Err(e) => {
            eprintln!("peer map config error: {e}");
            return;
        }
    };
    let st = Arc::new(St {
        id: env::var("DEMO_CLIENT_ID").unwrap_or_else(|_| "rs".to_string()),
        path: env::var("DEMO_HTTP_PATH").unwrap_or_else(|_| "/v1/pipeline".to_string()),
        peers,
        max_proc: max_processing_sec(),
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
    axum::serve(l, app)
        .await
        .expect("serve");
    let _ = keep.shutdown();
}
