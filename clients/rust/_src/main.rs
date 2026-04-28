//! HTTP pipeline (Rust) — W3C propagate, forward.
use std::env;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::StatusCode;
use axum::routing::post;
use axum::response::IntoResponse;
use axum::response::Response;
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

#[derive(Deserialize, Serialize, Clone, Debug)]
struct Msg {
    counter: String,
    table_of_clients: Vec<String>,
}

struct St {
    next: Option<String>,
    id: String,
    path: String,
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

fn bump(m: &mut Msg, cid: &str) {
    let c: i32 = m.counter.parse().unwrap_or(0) + 1;
    m.counter = c.to_string();
    m.table_of_clients.push(cid.to_string());
}

async fn pipeline(
    State(st): State<Arc<St>>,
    headers: HeaderMap,
    body: Bytes,
) -> axum::response::Response {
    use axum::body::Body;

    let parent: Context =
        global::get_text_map_propagator(|p| p.extract(&HeaderExtractor(&headers)));
    let t = global::tracer_with_scope(
        InstrumentationScope::builder("demo_app")
            .with_version("1.0.0")
            .build(),
    );

    let mut m: Msg = match serde_json::from_slice(&body) {
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
    bump(&mut m, &st.id);

    if let Some(ref u) = st.next {
        let ser = match serde_json::to_vec(&m) {
            Ok(b) => b,
            Err(e) => {
                return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
            }
        };
        rs_line(&format!(
            "[{}] forward to {u}: {}",
            st.id,
            String::from_utf8_lossy(&ser)
        ));
        let hop = t.start_with_context("pipeline.hop", &parent);
        let hop_cx = parent.clone().with_span(hop);
        let forward = t.start_with_context("pipeline.forward", &hop_cx);
        let forward_cx = hop_cx.clone().with_span(forward);

        let mut hmap = http::HeaderMap::new();
        global::get_text_map_propagator(|p| {
            p.inject_context(&forward_cx, &mut HeaderInjector(&mut hmap))
        });

        let cl = reqwest::Client::new();
        let mut rb = cl.post(u).body(ser);
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

    let hop = t.start_with_context("pipeline.hop", &parent);
    let _hop_cx = parent.clone().with_span(hop);
    let out = match serde_json::to_string(&m) {
        Ok(x) => x,
        Err(e) => {
            return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
        }
    };
    rs_line(&format!(
        "[{}] respond (terminal, brak forward): {out}",
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
    let st = Arc::new(St {
        next: env::var("DEMO_NEXT_URL").ok().and_then(|s| {
            let t = s.trim();
            if t.is_empty() {
                None
            } else {
                Some(t.to_string())
            }
        }),
        id: env::var("DEMO_CLIENT_ID").unwrap_or_else(|_| "rs".to_string()),
        path: env::var("DEMO_HTTP_PATH").unwrap_or_else(|_| "/v1/pipeline".to_string()),
    });
    let a: SocketAddr = env::var("DEMO_HTTP_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:8080".to_string())
        .parse()
        .expect("DEMO_HTTP_ADDR");
    let app = Router::new()
        .route(st.path.as_str(), post(pipeline))
        .with_state(st.clone());
    let l = TcpListener::bind(a).await.expect("bind");
    rs_line(&format!("Rust pipeline http://{a}{} next={:?}", st.path, st.next));
    axum::serve(l, app)
        .await
        .expect("serve");
    let _ = keep.shutdown();
}
