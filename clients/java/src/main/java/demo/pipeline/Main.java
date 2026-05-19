package demo.pipeline;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributesBuilder;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.Scope;
import io.opentelemetry.context.propagation.ContextPropagators;
import io.opentelemetry.context.propagation.TextMapGetter;
import io.opentelemetry.context.propagation.TextMapPropagator;
import io.opentelemetry.context.propagation.TextMapSetter;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Pipeline worker HTTP (POST JSON) — kontrakt jak Python/Go/Rust. Domyślnie {@code DEMO_CLIENT_ID=ja} (segment {@code "id":"ja"} w trasie).
 */
public final class Main {

  private static final Gson GSON = new Gson();
  private static final Pattern RE_SEC = Pattern.compile("^\\s*(\\d+(?:\\.\\d+)?)\\s*s\\s*$", Pattern.CASE_INSENSITIVE);
  private static final Pattern RE_MS = Pattern.compile("^\\s*(\\d+(?:\\.\\d+)?)\\s*ms\\s*$", Pattern.CASE_INSENSITIVE);

  private static final TextMapGetter<HttpExchange> HTTP_GETTER =
      new TextMapGetter<>() {
        @Override
        public Iterable<String> keys(HttpExchange carrier) {
          return carrier.getRequestHeaders().keySet();
        }

        @Override
        public String get(HttpExchange carrier, String key) {
          return carrier.getRequestHeaders().getFirst(key);
        }
      };

  private static final TextMapSetter<Map<String, String>> MAP_SETTER = Map::put;

  public static void main(String[] args) throws Exception {
    String mode = env("DEMO_MODE", "pipeline").toLowerCase(Locale.ROOT);
    if ("exercises".equals(mode)) {
      System.err.println("DEMO_MODE=exercises — użyj DEMO_MODE=pipeline.");
      return;
    }

    String tracesEp = env("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces");
    String svc = env("OTEL_SERVICE_NAME", "worker_java");
    String envName = firstNonBlank(env("OTEL_ENVIRONMENT", ""), env("DEPLOYMENT_ENVIRONMENT", "local"));
    String tag = env("OTEL_DEMO_RESOURCE_TAG", "").trim();
    String iid = env("OTEL_SERVICE_INSTANCE_ID", "").trim();
    if (iid.isEmpty()) {
      iid = UUID.randomUUID().toString();
    }

    AttributesBuilder rb = Attributes.builder();
    rb.put("service.name", svc);
    rb.put("service.version", "1.0.0");
    rb.put("service.instance.id", iid);
    rb.put("deployment.environment", envName);
    rb.put("telemetry.sdk.language", "java");
    rb.put("telemetry.sdk.name", "opentelemetry");
    if (!tag.isEmpty()) {
      rb.put("demo.instance.tag", tag);
    }
    Resource resource = Resource.getDefault().merge(Resource.create(rb.build()));

    OtlpHttpSpanExporter spanExporter =
        OtlpHttpSpanExporter.builder()
            .setEndpoint(tracesEp)
            .setTimeout(Duration.ofSeconds(30))
            .build();

    SdkTracerProvider tracerProvider =
        SdkTracerProvider.builder()
            .setResource(resource)
            .addSpanProcessor(BatchSpanProcessor.builder(spanExporter).build())
            .build();

    TextMapPropagator propagator = W3CTraceContextPropagator.getInstance();
    OpenTelemetrySdk sdk =
        OpenTelemetrySdk.builder()
            .setTracerProvider(tracerProvider)
            .setPropagators(ContextPropagators.create(propagator))
            .buildAndRegisterGlobal();

    Runtime.getRuntime()
        .addShutdownHook(
            new Thread(
                () -> {
                  try {
                    sdk.getSdkTracerProvider().shutdown().join(10, java.util.concurrent.TimeUnit.SECONDS);
                  } catch (Exception ignored) {
                  }
                }));

    String httpAddr = env("DEMO_HTTP_ADDR", "0.0.0.0:8080");
    String httpPath = env("DEMO_HTTP_PATH", "/v1/pipeline");
    String clientId = env("DEMO_CLIENT_ID", "ja");
    Map<String, String> peers = loadPeerMap();
    double maxProc = parseMaxProcessingSec();

    int port = parsePort(httpAddr);
    String host = parseHost(httpAddr);
    HttpServer server = HttpServer.create(new InetSocketAddress(host, port), 0);
    Tracer tracer = sdk.getTracer("demo.pipeline", "1.0.0");
    server.createContext(
        httpPath,
        new PipelineHandler(tracer, propagator, clientId, peers, maxProc, httpPath));
    server.setExecutor(null);
    server.start();
    System.out.println(
        "pipeline: listen http://" + httpAddr + httpPath + " client_id=" + clientId + " peers=" + peers.keySet());
  }

  private static String env(String k, String d) {
    String v = System.getenv(k);
    return v != null && !v.trim().isEmpty() ? v.trim() : d;
  }

  private static String firstNonBlank(String a, String b) {
    if (a != null && !a.trim().isEmpty()) {
      return a.trim();
    }
    return b != null ? b.trim() : "";
  }

  private static double parseMaxProcessingSec() {
    try {
      double n = Double.parseDouble(env("DEMO_MAX_PROCESSING_SEC", "120"));
      return n >= 0 && !Double.isNaN(n) ? n : 120;
    } catch (NumberFormatException e) {
      return 120;
    }
  }

  private static String parseHost(String addr) {
    int i = addr.lastIndexOf(':');
    if (i < 0) {
      return "0.0.0.0";
    }
    String h = addr.substring(0, i).trim();
    return h.isEmpty() ? "0.0.0.0" : h;
  }

  private static int parsePort(String addr) {
    int i = addr.lastIndexOf(':');
    if (i < 0) {
      return 8080;
    }
    try {
      return Integer.parseInt(addr.substring(i + 1).trim());
    } catch (NumberFormatException e) {
      return 8080;
    }
  }

  private static Map<String, String> loadPeerMap() {
    String raw = env("DEMO_PEER_MAP", "").trim();
    Map<String, String> out = new HashMap<>();
    if (!raw.isEmpty()) {
      JsonObject o = JsonParse.parse(raw).getAsJsonObject();
      for (Map.Entry<String, JsonElement> e : o.entrySet()) {
        String k = e.getKey().trim().toLowerCase(Locale.ROOT);
        if (!e.getValue().isJsonPrimitive()) {
          continue;
        }
        String v = e.getValue().getAsString().trim();
        if (!k.isEmpty() && !v.isEmpty()) {
          out.put(k, v);
        }
      }
      return out;
    }
    for (Map.Entry<String, String> e : System.getenv().entrySet()) {
      String k = e.getKey();
      if (!k.startsWith("DEMO_PEER_") || "DEMO_PEER_MAP".equals(k)) {
        continue;
      }
      String tail = k.substring("DEMO_PEER_".length()).trim().toLowerCase(Locale.ROOT);
      String v = Optional.ofNullable(e.getValue()).orElse("").trim();
      if (!tail.isEmpty() && !v.isEmpty()) {
        out.put(tail, v);
      }
    }
    return out;
  }

  /** Parsowanie JSON bez zależności od {@code com.google.gson.JsonParser}. */
  private static final class JsonParse {
    static JsonElement parse(String s) {
      return GSON.fromJson(s, JsonElement.class);
    }
  }

  private static double parseProcessingTime(JsonElement timeEl, double capSec) {
    if (timeEl == null || timeEl.isJsonNull()) {
      return 0;
    }
    if (timeEl.isJsonPrimitive() && timeEl.getAsJsonPrimitive().isNumber()) {
      double sec = timeEl.getAsDouble();
      return Math.min(Math.max(0, sec), capSec);
    }
    String s = timeEl.getAsString().trim();
    if (s.isEmpty()) {
      return 0;
    }
    Matcher m1 = RE_SEC.matcher(s);
    if (m1.matches()) {
      return Math.min(Double.parseDouble(m1.group(1)), capSec);
    }
    Matcher m2 = RE_MS.matcher(s);
    if (m2.matches()) {
      return Math.min(Double.parseDouble(m2.group(1)) / 1000.0, capSec);
    }
    throw new IllegalArgumentException("invalid processing time: " + s);
  }

  private static void cpuSpin(double sec) {
    if (sec <= 0) {
      return;
    }
    long deadline = System.nanoTime() + (long) (sec * 1_000_000_000L);
    int v = 1;
    while (System.nanoTime() < deadline) {
      for (int i = 0; i < 1024; i++) {
        v = (v * 1103515245 + 12345) & 0x7fffffff;
      }
    }
  }

  private static String peerUrl(Map<String, String> peers, String segId) {
    String nid = segId.trim().toLowerCase(Locale.ROOT);
    String u = peers.get(nid);
    if (u != null) {
      return u;
    }
    return peers.get(segId.trim());
  }

  private static int firstUnvisited(JsonArray route) {
    for (int i = 0; i < route.size(); i++) {
      JsonObject seg = route.get(i).getAsJsonObject();
      if (!seg.has("visited") || !seg.get("visited").getAsBoolean()) {
        return i;
      }
    }
    return -1;
  }

  private static void bumpCounter(JsonObject msg, String clientId) {
    int c = 0;
    try {
      c = Integer.parseInt(msg.has("counter") ? msg.get("counter").getAsString() : "0");
    } catch (NumberFormatException ignored) {
    }
    msg.addProperty("counter", String.valueOf(c + 1));
    JsonArray t = msg.has("table_of_clients") ? msg.getAsJsonArray("table_of_clients") : new JsonArray();
    if (!msg.has("table_of_clients")) {
      msg.add("table_of_clients", t);
    }
    t.add(clientId);
  }

  private static HttpResponse<byte[]> httpPost(String url, byte[] body, Map<String, String> headers)
      throws IOException, InterruptedException {
    HttpClient client =
        HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(30)).build();
    HttpRequest.Builder b =
        HttpRequest.newBuilder()
            .uri(URI.create(url))
            .timeout(Duration.ofMinutes(2))
            .POST(HttpRequest.BodyPublishers.ofByteArray(body));
    headers.forEach(b::header);
    if (headers.get("Content-Type") == null) {
      b.header("Content-Type", "application/json");
    }
    return client.send(b.build(), HttpResponse.BodyHandlers.ofByteArray());
  }

  private static final class PipelineHandler implements HttpHandler {
    private final Tracer tracer;
    private final TextMapPropagator propagator;
    private final String clientId;
    private final Map<String, String> peers;
    private final double maxProcSec;
    private final String httpPath;

    PipelineHandler(
        Tracer tracer,
        TextMapPropagator propagator,
        String clientId,
        Map<String, String> peers,
        double maxProcSec,
        String httpPath) {
      this.tracer = tracer;
      this.propagator = propagator;
      this.clientId = clientId;
      this.peers = peers;
      this.maxProcSec = maxProcSec;
      this.httpPath = httpPath;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
      String p = ex.getRequestURI().getPath();
      if (!p.equals(httpPath) && !p.replaceAll("/$", "").equals(httpPath.replaceAll("/$", ""))) {
        ex.sendResponseHeaders(404, -1);
        return;
      }
      if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
        ex.sendResponseHeaders(405, -1);
        return;
      }
      byte[] raw;
      try (InputStream in = ex.getRequestBody()) {
        raw = in.readAllBytes();
      }
      Context parent = propagator.extract(Context.current(), ex, HTTP_GETTER);
      try (Scope scope = parent.makeCurrent()) {
        Span hop =
            tracer
                .spanBuilder("pipeline.hop")
                .setSpanKind(SpanKind.SERVER)
                .setAttribute("demo.client_id", clientId)
                .startSpan();
        try (Scope hs = hop.makeCurrent()) {
          handleHop(ex, raw, hop);
        } catch (Exception e) {
          hop.recordException(e);
          hop.setStatus(StatusCode.ERROR, e.getMessage());
          if (!ex.getResponseHeaders().containsKey("Content-Type")) {
            byte[] err = (e.getMessage() != null ? e.getMessage() : "error").getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().set("Content-Type", "text/plain; charset=utf-8");
            ex.sendResponseHeaders(502, err.length);
            try (OutputStream os = ex.getResponseBody()) {
              os.write(err);
            }
          }
        } finally {
          hop.end();
        }
      }
    }

    private void handleHop(HttpExchange ex, byte[] raw, Span hop) throws Exception {
      JsonObject msg;
      try {
        msg = JsonParse.parse(new String(raw, StandardCharsets.UTF_8)).getAsJsonObject();
      } catch (Exception e) {
        ex.sendResponseHeaders(400, -1);
        return;
      }
      System.out.println("[" + clientId + "] received: " + new String(raw, StandardCharsets.UTF_8));
      if (!msg.has("route") || !msg.get("route").isJsonArray() || msg.getAsJsonArray("route").size() == 0) {
        ex.sendResponseHeaders(400, -1);
        return;
      }
      JsonArray route = msg.getAsJsonArray("route");
      int idx = firstUnvisited(route);
      if (idx < 0) {
        ex.sendResponseHeaders(400, -1);
        return;
      }
      JsonObject seg = route.get(idx).getAsJsonObject();
      if (!clientId.equalsIgnoreCase(Optional.ofNullable(seg.get("id")).map(JsonElement::getAsString).orElse("").trim())) {
        ex.sendResponseHeaders(400, -1);
        return;
      }
      if (seg.has("processing_time")) {
        ex.sendResponseHeaders(400, -1);
        return;
      }
      if (!seg.has("processing_steps") || !seg.get("processing_steps").isJsonArray()) {
        ex.sendResponseHeaders(400, -1);
        return;
      }
      JsonArray steps = seg.getAsJsonArray("processing_steps");
      if (steps.size() == 0) {
        ex.sendResponseHeaders(400, -1);
        return;
      }

      Span proc =
          tracer
              .spanBuilder("pipeline.processing")
              .setAttribute("demo.processing.mode", "steps")
              .setAttribute("demo.step_count", (long) steps.size())
              .startSpan();
      try (Scope ps = proc.makeCurrent()) {
        for (int si = 0; si < steps.size(); si++) {
          JsonObject step = steps.get(si).getAsJsonObject();
          String activity = step.has("activity") ? step.get("activity").getAsString() : ("step_" + si);
          if (step.has("nested_route")) {
            double pre = parseProcessingTime(step.get("time"), maxProcSec);
            JsonArray nr = step.getAsJsonArray("nested_route");
            if (nr.size() == 0) {
              throw new IllegalArgumentException("nested_route empty");
            }
            String firstId = nr.get(0).getAsJsonObject().get("id").getAsString().trim();
            if (firstId.equalsIgnoreCase(clientId)) {
              throw new IllegalArgumentException("nested_route[0] must not be this node");
            }
            Span st =
                tracer
                    .spanBuilder(activity.length() > 200 ? activity.substring(0, 200) : activity)
                    .setAttribute("demo.activity", activity)
                    .setAttribute("demo.cpu_spin_sec", pre)
                    .setAttribute("demo.step_index", (long) si)
                    .setAttribute("demo.nested_subpipeline", true)
                    .startSpan();
            try (Scope ss = st.makeCurrent()) {
              cpuSpin(pre);
              String nurl = peerUrl(peers, firstId);
              if (nurl == null) {
                throw new IllegalStateException("no peer URL for nested id " + firstId);
              }
              JsonObject nestBody = nestedPayload(nr);
              byte[] nb = GSON.toJson(nestBody).getBytes(StandardCharsets.UTF_8);
              System.out.println("[" + clientId + "] nested_forward to " + nurl + ": " + new String(nb, StandardCharsets.UTF_8));
              Span nf =
                  tracer
                      .spanBuilder("pipeline.nested_forward")
                      .setSpanKind(SpanKind.CLIENT)
                      .setAttribute("http.url", nurl.trim())
                      .startSpan();
              try (Scope ns = nf.makeCurrent()) {
                Map<String, String> hdr = new HashMap<>();
                propagator.inject(Context.current(), hdr, MAP_SETTER);
                HttpResponse<byte[]> r = httpPost(nurl.trim(), nb, hdr);
                nf.setAttribute("demo.downstream_status", (long) r.statusCode());
                if (r.statusCode() < 200 || r.statusCode() >= 300) {
                  throw new IOException("nested_forward HTTP " + r.statusCode());
                }
              } finally {
                nf.end();
              }
            } finally {
              st.end();
            }
          } else {
            double sec = parseProcessingTime(step.get("time"), maxProcSec);
            String name = activity.isEmpty() ? "pipeline.processing_step" : activity;
            Span st = tracer.spanBuilder(name).startSpan();
            try (Scope ss = st.makeCurrent()) {
              st.setAttribute("demo.activity", activity);
              st.setAttribute("demo.cpu_spin_sec", sec);
              st.setAttribute("demo.step_index", (long) si);
              cpuSpin(sec);
            } finally {
              st.end();
            }
          }
        }
      } finally {
        proc.end();
      }

      seg.addProperty("visited", true);
      if (!msg.has("visit_log") || !msg.get("visit_log").isJsonArray()) {
        msg.add("visit_log", new JsonArray());
      }
      msg.getAsJsonArray("visit_log").add(clientId);
      bumpCounter(msg, clientId);

      int nextIdx = firstUnvisited(route);
      hop.setAttribute("demo.counter", msg.get("counter").getAsString());
      hop.setAttribute("demo.table_len", (long) msg.getAsJsonArray("table_of_clients").size());
      hop.setAttribute("demo.has_forward", nextIdx >= 0);

      byte[] outBody = GSON.toJson(msg).getBytes(StandardCharsets.UTF_8);
      if (nextIdx < 0) {
        System.out.println("[" + clientId + "] respond (terminal route): " + new String(outBody, StandardCharsets.UTF_8));
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(200, outBody.length);
        try (OutputStream os = ex.getResponseBody()) {
          os.write(outBody);
        }
        return;
      }
      String nextId = route.get(nextIdx).getAsJsonObject().get("id").getAsString().trim();
      String nextUrl = peerUrl(peers, nextId);
      if (nextUrl == null) {
        ex.sendResponseHeaders(502, -1);
        return;
      }
      System.out.println("[" + clientId + "] forward to " + nextUrl + ": " + new String(outBody, StandardCharsets.UTF_8));
      Span fw =
          tracer
              .spanBuilder("pipeline.forward")
              .setSpanKind(SpanKind.CLIENT)
              .setAttribute("http.url", nextUrl.trim())
              .startSpan();
      try (Scope fs = fw.makeCurrent()) {
        Map<String, String> hdr = new HashMap<>();
        propagator.inject(Context.current(), hdr, MAP_SETTER);
        HttpResponse<byte[]> r = httpPost(nextUrl.trim(), outBody, hdr);
        fw.setAttribute("demo.downstream_status", (long) r.statusCode());
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(r.statusCode(), r.body() == null ? 0 : r.body().length);
        if (r.body() != null) {
          try (OutputStream os = ex.getResponseBody()) {
            os.write(r.body());
          }
        }
      } finally {
        fw.end();
      }
    }

    private JsonObject nestedPayload(JsonArray nestedRoute) {
      JsonArray nr = new JsonArray();
      for (JsonElement e : nestedRoute) {
        JsonObject s = e.getAsJsonObject().deepCopy();
        s.addProperty("visited", false);
        nr.add(s);
      }
      JsonObject o = new JsonObject();
      o.add("route", nr);
      o.add("visit_log", new JsonArray());
      o.addProperty("counter", "0");
      o.add("table_of_clients", new JsonArray());
      return o;
    }
  }
}
