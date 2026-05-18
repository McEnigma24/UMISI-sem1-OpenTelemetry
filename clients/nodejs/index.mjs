/**
 * HTTP pipeline worker (Node.js) — W3C, processing_steps, nested_route (jak Python/Rust).
 */
import http from "node:http";
import os from "node:os";
import { randomUUID } from "node:crypto";

import {
  context,
  propagation,
  trace,
  metrics,
  ROOT_CONTEXT,
  SpanKind,
  SpanStatusCode,
} from "@opentelemetry/api";
import { logs, SeverityNumber } from "@opentelemetry/api-logs";
import { NodeSDK } from "@opentelemetry/sdk-node";
import { Resource } from "@opentelemetry/resources";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { OTLPLogExporter } from "@opentelemetry/exporter-logs-otlp-http";
import {
  LoggerProvider,
  BatchLogRecordProcessor,
} from "@opentelemetry/sdk-logs";
import {
  CompositePropagator,
  W3CBaggagePropagator,
  W3CTraceContextPropagator,
} from "@opentelemetry/core";
import { AlwaysOnSampler } from "@opentelemetry/sdk-trace-base";
import { SEMRESATTRS_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

/** TextMapGetter dla Node `req.headers` (bez importu CJS z @opentelemetry/core w ESM). */
const nodeHeaderGetter = {
  get(carrier, key) {
    if (!carrier || typeof carrier !== "object") return undefined;
    const v = carrier[key] ?? carrier[key.toLowerCase()];
    if (v === undefined) return undefined;
    return Array.isArray(v) ? v[0] : v;
  },
  keys(carrier) {
    return carrier && typeof carrier === "object" ? Object.keys(carrier) : [];
  },
};

function envOr(k, d) {
  const v = process.env[k];
  return v != null && String(v).trim() !== "" ? String(v).trim() : d;
}

function useOtlp() {
  const m = (process.env.OTEL_DEMO_TRACE_EXPORT || "").toLowerCase();
  if (m === "ostream") return false;
  return true;
}

function useOtlpLogExport() {
  if (!useOtlp()) return false;
  const v = (process.env.OTEL_DEMO_LOG_EXPORT || "otlp").toLowerCase();
  return !["0", "false", "no", "off"].includes(v);
}

function processMetricsEnabled() {
  const v = (process.env.DEMO_PROCESS_METRICS || "true").toLowerCase();
  return !["0", "false", "no", "off"].includes(v);
}

function otlpTracesEp() {
  return envOr(
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "http://127.0.0.1:4318/v1/traces"
  );
}

function otlpMetricsEp() {
  const o = process.env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT;
  if (o && String(o).trim()) return String(o).trim();
  return otlpTracesEp().replace("/v1/traces", "/v1/metrics");
}

function otlpLogsEp() {
  const o = process.env.OTEL_EXPORTER_OTLP_LOGS_ENDPOINT;
  if (o && String(o).trim()) return String(o).trim();
  return otlpTracesEp().replace("/v1/traces", "/v1/logs");
}

function maxProcessingSec() {
  const n = parseFloat(process.env.DEMO_MAX_PROCESSING_SEC || "120");
  return Number.isFinite(n) && n >= 0 ? n : 120;
}

function loadPeerMap() {
  const raw = (process.env.DEMO_PEER_MAP || "").trim();
  if (raw) {
    const obj = JSON.parse(raw);
    if (typeof obj !== "object" || obj === null || Array.isArray(obj)) {
      throw new Error("DEMO_PEER_MAP must be a JSON object");
    }
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      const kk = String(k).trim().toLowerCase();
      const vv = String(v).trim();
      if (kk && vv) out[kk] = vv;
    }
    return out;
  }
  const out = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (!k.startsWith("DEMO_PEER_") || k === "DEMO_PEER_MAP") continue;
    const tail = k.slice("DEMO_PEER_".length).trim().toLowerCase();
    const vv = String(v || "").trim();
    if (tail && vv) out[tail] = vv;
  }
  return out;
}

function parseProcessingTime(s, capSec) {
  if (s == null) return 0;
  const str = String(s).trim();
  if (!str) return 0;
  const low = str.toLowerCase();
  let sec;
  if (low.endsWith("ms")) {
    sec = parseFloat(low.slice(0, -2).trim()) / 1000;
  } else if (low.endsWith("s")) {
    sec = parseFloat(low.slice(0, -1).trim());
  } else {
    throw new Error(
      `invalid processing_time: ${JSON.stringify(s)} (use e.g. 5.6s or 100ms)`
    );
  }
  if (!Number.isFinite(sec) || sec < 0) {
    throw new Error("processing_time must be non-negative");
  }
  return Math.min(sec, capSec);
}

function segmentSchemaErr(seg, where) {
  if (seg.processing_time !== undefined) {
    return (
      `${where}: field 'processing_time' is not supported; use non-empty ` +
      `'processing_steps' with {"activity","time"}`
    );
  }
  const steps = seg.processing_steps;
  if (!Array.isArray(steps) || steps.length === 0) {
    return `${where}: non-empty 'processing_steps' is required`;
  }
  return null;
}

function validateNestedRoute(raw, stepI) {
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new Error(
      `processing_steps[${stepI}].nested_route must be a non-empty array`
    );
  }
  for (let j = 0; j < raw.length; j++) {
    const seg = raw[j];
    if (!seg || typeof seg !== "object") {
      throw new Error(
        `processing_steps[${stepI}].nested_route[${j}] must be an object`
      );
    }
    const sch = segmentSchemaErr(seg, `processing_steps[${stepI}].nested_route[${j}]`);
    if (sch) throw new Error(sch);
    if (!String(seg.id || "").trim()) {
      throw new Error(
        `processing_steps[${stepI}].nested_route[${j}].id must be a non-empty string`
      );
    }
  }
  const first = String(raw[0].id || "")
    .trim()
    .toLowerCase();
  if (first === "py") {
    throw new Error(
      `processing_steps[${stepI}].nested_route[0].id must not be 'py' (workers only)`
    );
  }
  return raw;
}

function parseProcessingSteps(seg, cap) {
  const raw = seg.processing_steps;
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const out = [];
  for (let i = 0; i < raw.length; i++) {
    const item = raw[i];
    if (!item || typeof item !== "object") {
      throw new Error(`processing_steps[${i}] must be an object`);
    }
    const act = typeof item.activity === "string" ? item.activity : `step_${i}`;
    if (item.nested_route != null) {
      const nr = validateNestedRoute(item.nested_route, i);
      const pre = parseProcessingTime(item.time, cap);
      out.push({ kind: "nested", activity: act, preSec: pre, nestedRoute: nr, index: i });
    } else {
      const sec = parseProcessingTime(item.time, cap);
      out.push({ kind: "cpu", activity: act, sec, index: i });
    }
  }
  return out;
}

function spanNameForActivity(activity) {
  const t = String(activity || "").trim();
  if (!t) return "pipeline.processing_step";
  return t.length > 256 ? t.slice(0, 256) : t;
}

function firstUnvisited(route) {
  for (let i = 0; i < route.length; i++) {
    if (!route[i].visited) return i;
  }
  return -1;
}

function peerURL(peers, segId) {
  const nid = String(segId || "").trim().toLowerCase();
  return peers[nid] || peers[String(segId || "").trim()] || "";
}

function bumpCounterTable(data, clientId) {
  const c = parseInt(String(data.counter || "0"), 10) || 0;
  data.counter = String(c + 1);
  if (!Array.isArray(data.table_of_clients)) data.table_of_clients = [];
  data.table_of_clients.push(clientId);
}

function nestedPipelinePayload(nestedRoute) {
  return {
    route: nestedRoute.map((s) => ({ ...s, visited: false })),
    visit_log: [],
    counter: "0",
    table_of_clients: [],
  };
}

function cpuSpin(sec) {
  if (sec <= 0) return;
  const deadline = Date.now() + sec * 1000;
  let v = 1;
  while (Date.now() < deadline) {
    for (let i = 0; i < 2048; i++) {
      v = (v * 1103515245 + 12345) & 0x7fffffff;
    }
  }
}

function buildResource() {
  const iid = envOr("OTEL_SERVICE_INSTANCE_ID", randomUUID());
  const envName = envOr(
    "OTEL_ENVIRONMENT",
    envOr("DEPLOYMENT_ENVIRONMENT", "local")
  );
  const svc = envOr("OTEL_SERVICE_NAME", "worker_nodejs");
  const tag = (process.env.OTEL_DEMO_RESOURCE_TAG || "").trim();
  const attrs = {
    [SEMRESATTRS_SERVICE_NAME]: svc,
    "service.version": "1.0.0",
    "service.instance.id": iid,
    "deployment.environment": envName,
    "host.name": os.hostname(),
    "telemetry.sdk.language": "nodejs",
    "telemetry.sdk.name": "opentelemetry",
  };
  if (tag) attrs["demo.instance.tag"] = tag;
  return Resource.default().merge(new Resource(attrs));
}

function processMetricAttrs() {
  return {
    "service.name": envOr("OTEL_SERVICE_NAME", "worker_nodejs"),
    "demo.client_id": envOr("DEMO_CLIENT_ID", "js"),
  };
}

function parseHttpAddr(addr) {
  const last = addr.lastIndexOf(":");
  if (last < 0) return { host: "0.0.0.0", port: 8080 };
  const host = addr.slice(0, last) || "0.0.0.0";
  const port = parseInt(addr.slice(last + 1), 10) || 8080;
  return { host, port };
}

function pipelinePointAttrs(clientId) {
  return { ...processMetricAttrs(), client_id: clientId };
}

async function httpPostJSON(url, bodyBytes, headerObj) {
  const headers = { ...headerObj, "content-type": "application/json" };
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 120_000);
  try {
    const res = await fetch(url, {
      method: "POST",
      body: bodyBytes,
      headers,
      signal: ac.signal,
    });
    const buf = Buffer.from(await res.arrayBuffer());
    return { status: res.status, body: buf };
  } finally {
    clearTimeout(t);
  }
}

async function startPyroscopeIfConfigured() {
  const srv = (process.env.PYROSCOPE_SERVER || "").trim();
  if (!srv) return;
  const en = (process.env.PYROSCOPE_ENABLED || "true").trim().toLowerCase();
  if (["0", "false", "no", "off"].includes(en)) return;
  const appName = envOr("OTEL_SERVICE_NAME", "worker_nodejs");
  try {
    const mod = await import("@pyroscope/nodejs");
    const Pyroscope = mod.default ?? mod;
    Pyroscope.init({
      serverAddress: srv,
      appName,
      tags: { demo_client_id: envOr("DEMO_CLIENT_ID", "js") },
      wall: { collectCpuTime: true },
    });
    Pyroscope.start();
    const ts = new Date().toISOString().slice(11, 23);
    console.warn(
      `${ts} Pyroscope push profiler: server='${srv}' application_name='${appName}'`
    );
  } catch (e) {
    console.warn("[nodejs] Pyroscope:", e?.message || e);
  }
}

async function runPipelineOnce({
  req,
  res,
  clientId,
  httpPath,
  peers,
  maxProc,
  tracer,
  hopHist,
  msgCounter,
  logLogger,
  t0,
}) {
  const finishHop = () => {
    hopHist.record(Date.now() - t0, pipelinePointAttrs(clientId));
  };

  const p = req.url.split("?")[0];
  if (p !== httpPath && p.replace(/\/$/, "") !== httpPath.replace(/\/$/, "")) {
    res.writeHead(404);
    res.end();
    return;
  }
  if (req.method !== "POST") {
    res.writeHead(405);
    res.end("method not allowed");
    return;
  }

  const chunks = [];
  for await (const c of req) chunks.push(c);
  const rawBody = Buffer.concat(chunks);

  function jsLine(msg) {
    const ts = new Date().toISOString().slice(11, 23);
    const line = `${ts} ${msg}`;
    console.log(line);
    if (logLogger) {
      logLogger.emit({
        severityNumber: SeverityNumber.INFO,
        severityText: "INFO",
        body: line,
      });
    }
  }

  let msg;
  try {
    msg = JSON.parse(rawBody.toString("utf8"));
  } catch {
    finishHop();
    res.writeHead(400);
    res.end("Invalid JSON");
    return;
  }
  if (!msg || typeof msg !== "object") {
    finishHop();
    res.writeHead(400);
    res.end("JSON must be an object");
    return;
  }

  jsLine(`[${clientId}] received: ${JSON.stringify(msg)}`);

  const parentCtx = propagation.extract(
    ROOT_CONTEXT,
    req.headers,
    nodeHeaderGetter
  );

  await context.with(parentCtx, async () => {
    await tracer.startActiveSpan(
      "pipeline.hop",
      { kind: SpanKind.SERVER, attributes: { "demo.client_id": clientId } },
      async (hopSpan) => {
        try {
          if (!Array.isArray(msg.route) || msg.route.length === 0) {
            hopSpan.setStatus({ code: SpanStatusCode.ERROR });
            finishHop();
            res.writeHead(400);
            res.end("JSON must include non-empty route array");
            return;
          }
          const idx = firstUnvisited(msg.route);
          if (idx < 0) {
            hopSpan.setStatus({ code: SpanStatusCode.ERROR });
            finishHop();
            res.writeHead(400);
            res.end("route has no unvisited segment");
            return;
          }
          const seg = msg.route[idx];
          if (String(seg.id || "").trim() !== clientId) {
            hopSpan.setStatus({ code: SpanStatusCode.ERROR });
            finishHop();
            res.writeHead(400);
            res.end(
              `first unvisited route segment id must match this node (expected ${JSON.stringify(clientId)}, got ${JSON.stringify(seg.id)})`
            );
            return;
          }
          const sch = segmentSchemaErr(seg, "route segment");
          if (sch) {
            hopSpan.setStatus({ code: SpanStatusCode.ERROR, message: sch });
            finishHop();
            res.writeHead(400);
            res.end(sch);
            return;
          }
          let steps;
          try {
            steps = parseProcessingSteps(seg, maxProc);
          } catch (e) {
            hopSpan.setStatus({
              code: SpanStatusCode.ERROR,
              message: String(e?.message || e),
            });
            finishHop();
            res.writeHead(400);
            res.end(String(e?.message || e));
            return;
          }
          if (!steps || steps.length === 0) {
            hopSpan.setStatus({ code: SpanStatusCode.ERROR });
            finishHop();
            res.writeHead(400);
            res.end("route segment: non-empty 'processing_steps' is required");
            return;
          }

          let cpuSum = 0;
          let hasNested = false;
          for (const s of steps) {
            if (s.kind === "cpu") cpuSum += s.sec;
            else {
              cpuSum += s.preSec;
              hasNested = true;
            }
          }

          await tracer.startActiveSpan(
            "pipeline.processing",
            {
              attributes: {
                "demo.processing.mode": "steps",
                "demo.step_count": steps.length,
                "demo.simulated_processing_sec": cpuSum,
                "demo.has_nested_steps": hasNested,
              },
            },
            async () => {
              for (const st of steps) {
                if (st.kind === "cpu") {
                  const name = spanNameForActivity(st.activity);
                  await tracer.startActiveSpan(
                    name,
                    {
                      attributes: {
                        "demo.activity": st.activity,
                        "demo.cpu_spin_sec": st.sec,
                        "demo.step_index": st.index,
                      },
                    },
                    () => {
                      cpuSpin(st.sec);
                    }
                  );
                } else {
                  const name = spanNameForActivity(st.activity);
                  await tracer.startActiveSpan(
                    name,
                    {
                      attributes: {
                        "demo.activity": st.activity,
                        "demo.cpu_spin_sec": st.preSec,
                        "demo.step_index": st.index,
                        "demo.nested_subpipeline": true,
                      },
                    },
                    async (stepSpan) => {
                      cpuSpin(st.preSec);
                      const firstId = String(st.nestedRoute[0].id || "").trim();
                      const nurl = peerURL(peers, firstId);
                      if (!nurl) {
                        stepSpan.setStatus({ code: SpanStatusCode.ERROR });
                        throw new Error(`no peer URL for nested id ${JSON.stringify(firstId)}`);
                      }
                      const nestBody = Buffer.from(
                        JSON.stringify(nestedPipelinePayload(st.nestedRoute)),
                        "utf8"
                      );
                      jsLine(
                        `[${clientId}] nested_forward to ${nurl}: ${nestBody.toString()}`
                      );

                      await tracer.startActiveSpan(
                        "pipeline.nested_forward",
                        {
                          kind: SpanKind.CLIENT,
                          attributes: { "http.url": nurl.trim() },
                        },
                        async (nfSpan) => {
                          const carrier = {};
                          propagation.inject(
                            trace.setSpan(context.active(), stepSpan),
                            carrier,
                            {
                              set(h, k, v) {
                                h[k] = v;
                              },
                            }
                          );
                          const r = await httpPostJSON(
                            nurl.trim(),
                            nestBody,
                            carrier
                          );
                          nfSpan.setAttribute("demo.downstream_status", r.status);
                          if (r.status < 200 || r.status >= 300) {
                            nfSpan.setStatus({
                              code: SpanStatusCode.ERROR,
                              message: `HTTP ${r.status}`,
                            });
                            throw new Error(`nested_forward HTTP ${r.status}`);
                          }
                        }
                      );
                    }
                  );
                }
              }
            }
          );

          seg.visited = true;
          if (!Array.isArray(msg.visit_log)) msg.visit_log = [];
          msg.visit_log.push(clientId);
          bumpCounterTable(msg, clientId);

          const nextIdx = firstUnvisited(msg.route);
          const outBody = Buffer.from(JSON.stringify(msg), "utf8");

          hopSpan.setAttribute("demo.counter", String(msg.counter || ""));
          hopSpan.setAttribute(
            "demo.table_len",
            (msg.table_of_clients || []).length
          );
          hopSpan.setAttribute("demo.has_forward", nextIdx >= 0);

          if (nextIdx < 0) {
            jsLine(
              `[${clientId}] respond (terminal route): ${outBody.toString()}`
            );
            msgCounter.add(1, pipelinePointAttrs(clientId));
            finishHop();
            res.writeHead(200, { "content-type": "application/json" });
            res.end(outBody);
            return;
          }

          const nextId = String(msg.route[nextIdx].id || "").trim();
          const nextURL = peerURL(peers, nextId);
          if (!nextURL) {
            finishHop();
            res.writeHead(502);
            res.end(`no peer URL for id ${JSON.stringify(nextId)}`);
            return;
          }
          jsLine(
            `[${clientId}] forward to ${nextURL}: ${outBody.toString()}`
          );

          await tracer.startActiveSpan(
            "pipeline.forward",
            {
              kind: SpanKind.CLIENT,
              attributes: { "http.url": nextURL.trim() },
            },
            async (fwSpan) => {
              const carrier = {};
              propagation.inject(
                trace.setSpan(context.active(), hopSpan),
                carrier,
                {
                  set(h, k, v) {
                    h[k] = v;
                  },
                }
              );
              const r = await httpPostJSON(nextURL.trim(), outBody, carrier);
              fwSpan.setAttribute("demo.downstream_status", r.status);
              msgCounter.add(1, pipelinePointAttrs(clientId));
              finishHop();
              res.writeHead(r.status, { "content-type": "application/json" });
              res.end(r.body);
            }
          );
        } catch (e) {
          hopSpan.recordException(e);
          hopSpan.setStatus({
            code: SpanStatusCode.ERROR,
            message: String(e?.message || e),
          });
          finishHop();
          if (!res.headersSent) {
            res.writeHead(502);
            res.end(String(e?.message || e));
          }
        }
      }
    );
  });
}

async function main() {
  if ((process.env.DEMO_MODE || "").toLowerCase() === "exercises") {
    console.log("DEMO_MODE=exercises — użyj DEMO_MODE=pipeline.");
    return;
  }

  const resource = buildResource();
  let sdk;
  let logProvider;

  if (useOtlp()) {
    /** Jak Rust: jawny W3C — inaczej przy nietypowym OTEL_PROPAGATORS extract/inject może być noop. */
    const textMapPropagator = new CompositePropagator({
      propagators: [
        new W3CTraceContextPropagator(),
        new W3CBaggagePropagator(),
      ],
    });
    sdk = new NodeSDK({
      resource,
      traceExporter: new OTLPTraceExporter({ url: otlpTracesEp() }),
      metricReader: new PeriodicExportingMetricReader({
        exporter: new OTLPMetricExporter({ url: otlpMetricsEp() }),
        exportIntervalMillis: 5000,
      }),
      textMapPropagator,
      /** ParentBased: remote z traceparent „nie próbkuj” → domyślnie AlwaysOff — spanów nie widać w Jaegerze. */
      sampler: new AlwaysOnSampler(),
    });
    sdk.start();

    if (useOtlpLogExport()) {
      logProvider = new LoggerProvider({ resource });
      logProvider.addLogRecordProcessor(
        new BatchLogRecordProcessor(
          new OTLPLogExporter({ url: otlpLogsEp() })
        )
      );
      logs.setGlobalLoggerProvider(logProvider);
    }
  }

  const svcName = envOr("OTEL_SERVICE_NAME", "worker_nodejs");
  const meter = metrics.getMeter(svcName, "1.0.0");
  const hopHist = meter.createHistogram("demo.pipeline.hop.duration_ms", {
    unit: "ms",
    description: "Czas przetworzenia i ewent. forward jednego hopy",
  });
  const msgCounter = meter.createCounter("demo.pipeline.messages", {
    description: "Liczba przetworzonych wiadomości w węźle",
  });

  if (useOtlp() && processMetricsEnabled()) {
    const attrs = processMetricAttrs();
    let lastCpu = process.cpuUsage();
    let lastTs = Date.now();
    const gCpu = meter.createObservableGauge(
      "demo.process.cpu.utilization",
      { unit: "%", description: "Użycie CPU procesu 0–100 (przybliżenie)" }
    );
    gCpu.addCallback((obs) => {
      const now = Date.now();
      const cur = process.cpuUsage(lastCpu);
      const dt = (now - lastTs) * 1000;
      if (dt > 0) {
        const used = cur.user + cur.system;
        const pct = Math.min(100, (used / dt) * 100);
        obs.observe(pct, attrs);
      }
      lastCpu = process.cpuUsage();
      lastTs = now;
    });
    const gMem = meter.createObservableGauge(
      "demo.process.memory.usage",
      { unit: "By", description: "RSS procesu (bajty)" }
    );
    gMem.addCallback((obs) => {
      obs.observe(process.memoryUsage().rss, attrs);
    });
  }

  await startPyroscopeIfConfigured();

  const peers = loadPeerMap();
  const clientId = envOr("DEMO_CLIENT_ID", "js");
  const httpPath = envOr("DEMO_HTTP_PATH", "/v1/pipeline");
  const addr = envOr("DEMO_HTTP_ADDR", "0.0.0.0:8080");
  const maxProc = maxProcessingSec();
  const { host, port } = parseHttpAddr(addr);

  const tracer = trace.getTracer(svcName, "1.0.0");
  const logLogger =
    useOtlpLogExport() && logProvider
      ? logs.getLogger("demo.pipeline", "1.0.0")
      : null;

  const server = http.createServer((req, res) => {
    const t0 = Date.now();
    runPipelineOnce({
      req,
      res,
      clientId,
      httpPath,
      peers,
      maxProc,
      tracer,
      hopHist,
      msgCounter,
      logLogger,
      t0,
    }).catch((e) => {
      console.error(e);
      try {
        hopHist.record(Date.now() - t0, pipelinePointAttrs(clientId));
      } catch {
        /* noop */
      }
      if (!res.headersSent) {
        res.writeHead(500);
        res.end(String(e));
      }
    });
  });

  await new Promise((resolve, reject) => {
    server.listen(port, host, (err) => {
      if (err) reject(err);
      else resolve();
    });
  });

  const ts = new Date().toISOString().slice(11, 23);
  console.log(
    `${ts} pipeline: listen http://${addr}${httpPath} client_id=${JSON.stringify(clientId)} peers=${JSON.stringify(Object.keys(peers))}`
  );
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
