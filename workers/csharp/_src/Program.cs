/*
 * HTTP pipeline (C#). POST DEMO_HTTP_PATH — JSON z ``route``.
 * ``processing_steps`` mogą zawierać ``nested_route`` (tylko workery; pierwszy ``id`` ≠ ``py``) + opcj. ``time`` (CPU przed POST).
 *
 * DEMO_VERBOSE_FRAMEWORK_LOGS — true/1/yes: więcej Microsoft/System.Net.Http/OpenTelemetry;
 *   domyślnie (false) — Warning+ dla frameworka i wyciszenie OTLP HttpClient (info).
 * DEMO_PROCESS_METRICS — false/0/no/off: nie rejestruj gauge demo.process.* (domyślnie włączone przy OTLP).
 * PYROSCOPE_SERVER + PYROSCOPE_ENABLED — ten sam komunikat „Pyroscope push profiler” co Python/Rust/Go/Node;
 *   w tym obrazie brak natywnego CorProfilera .NET (push profilu CPU wymaga osobnego obrazu / zmiennych CORECLR_*).
 */
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using System.Text.Json.Serialization.Metadata;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace OtelDemo;

public static class Program
{
    private static readonly ActivitySource Act = new("worker_csharp", "1.0.0");
    private static ILogger? s_pipelineLog;
    /// <summary>Żyjący uchwyt do bieżącego procesu — nie używamy <c>using</c> na <see cref="Process.GetCurrentProcess"/>,
    /// bo zwalnianie psuje kolejne odczyty metryk.</summary>
    private static readonly Process s_metricProcess = Process.GetCurrentProcess();
    private static readonly object s_cpuGaugeLock = new();
    private static long s_lastCpuGaugeWallTicks;
    private static long s_lastCpuGaugeCpuTicks;
    private static double s_lastPublishedCpuPct;
    private static readonly JsonSerializerOptions s_pipelineJson = new()
    {
        WriteIndented = false,
        TypeInfoResolver = new DefaultJsonTypeInfoResolver(),
    };

    /// <summary>Pipeline log: jeden kanał do konsoli (provider z hosta) + OTLP przez <c>AddOpenTelemetry</c> — bez podwójnego <c>Console.WriteLine</c> + ILogger.</summary>
    private static void CsLine(string m)
    {
        var line = $"{DateTime.Now:HH:mm:ss.fff} {m}";
        if (s_pipelineLog is not null)
            s_pipelineLog.LogWarning("{Line}", line);
        else
            Console.WriteLine(line);
    }

    private static bool PyroscopePushLogLineEnabled()
    {
        var v = GetLo("PYROSCOPE_ENABLED", "true").Trim();
        if (v is "0" or "false" or "no" or "off")
            return false;
        return GetLo("PYROSCOPE_SERVER", "").Trim().Length > 0;
    }

    // Ten sam tekst co Python/Rust; push CPU do Pyroscope w .NET = natywny CorProfiler (wyłączony w tym demo).
    private static void LogPyroscopePushLineIfConfigured()
    {
        if (!PyroscopePushLogLineEnabled())
            return;
        var server = GetLo("PYROSCOPE_SERVER", "").Trim();
        var app = GetLo("OTEL_SERVICE_NAME", "").Trim();
        if (app.Length == 0)
            app = "worker_csharp";
        CsLine($"Pyroscope push profiler: server='{server}' application_name='{app}'");
    }

    private static string NodeToJsonString(JsonNode? node) =>
        node is null ? "{}" : JsonSerializer.Serialize(node, s_pipelineJson);

    private static string GetLo(string k, string d) => Environment.GetEnvironmentVariable(k) ?? d;

    private static bool VerboseFrameworkLogs()
    {
        var v = GetLo("DEMO_VERBOSE_FRAMEWORK_LOGS", "false").Trim();
        return v.Equals("true", StringComparison.OrdinalIgnoreCase)
               || v is "1" or "yes";
    }

    private static string HostN() =>
        string.IsNullOrEmpty(Environment.GetEnvironmentVariable("HOSTNAME"))
            ? Dns.GetHostName()
            : Environment.GetEnvironmentVariable("HOSTNAME")!;

    private static ResourceBuilder ResB()
    {
        var iid = Environment.GetEnvironmentVariable("OTEL_SERVICE_INSTANCE_ID")
                  ?? Guid.NewGuid().ToString("N");
        var env = Environment.GetEnvironmentVariable("OTEL_ENVIRONMENT")
                  ?? Environment.GetEnvironmentVariable("DEPLOYMENT_ENVIRONMENT")
                  ?? "local";
        var tag = Environment.GetEnvironmentVariable("OTEL_DEMO_RESOURCE_TAG");
        var b = ResourceBuilder.CreateDefault()
            .AddService("worker_csharp", "1.0.0", autoGenerateServiceInstanceId: false)
            .AddAttributes(
                new Dictionary<string, object>
                {
                    ["service.instance.id"] = iid,
                    ["deployment.environment"] = env,
                    ["host.name"] = HostN(),
                    ["telemetry.sdk.language"] = "csharp",
                    ["telemetry.sdk.name"] = "opentelemetry",
                });
        if (!string.IsNullOrEmpty(tag))
        {
            b = b.AddAttributes(new Dictionary<string, object> { ["demo.instance.tag"] = tag! });
        }

        return b;
    }

    private static bool UseOtlp() => GetLo("OTEL_DEMO_TRACE_EXPORT", "") is "" or "otlp" or "http";
    private static bool UseOtlLogExport()
    {
        if (!UseOtlp())
            return false;
        var v = GetLo("OTEL_DEMO_LOG_EXPORT", "otlp").Trim().ToLowerInvariant();
        return v is not ("0" or "false" or "no" or "off");
    }

    private static string TrEp() => GetLo("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces");
    private static string MxEp() => GetLo("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", TrEp().Replace("/v1/traces", "/v1/metrics", StringComparison.Ordinal));
    private static string LogEp() => GetLo("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", TrEp().Replace("/v1/traces", "/v1/logs", StringComparison.Ordinal));

    private static double MaxProcSec() =>
        double.TryParse(GetLo("DEMO_MAX_PROCESSING_SEC", "120"), CultureInfo.InvariantCulture, out var x)
            ? x
            : 120.0;

    private static bool ProcessMetricsEnabled()
    {
        var v = GetLo("DEMO_PROCESS_METRICS", "true").Trim().ToLowerInvariant();
        return v is not ("0" or "false" or "no" or "off");
    }

    /// <summary>Monotonic busy-wait for demo CPU load (replaces Task.Delay for processing simulation).</summary>
    private static void CpuSpinForSeconds(double seconds)
    {
        if (seconds <= 0)
            return;
        var sw = Stopwatch.StartNew();
        long x = 1;
        while (sw.Elapsed.TotalSeconds < seconds)
        {
            for (var i = 0; i < 2048; i++)
                x = x * 1103515245 + 12345;
        }
    }

    private static void RegisterProcessMetrics(Meter m, string serviceName, string workerId)
    {
        var tags = new[]
        {
            new KeyValuePair<string, object?>("service.name", serviceName),
            new KeyValuePair<string, object?>("demo.worker_id", workerId),
        };

        m.CreateObservableGauge(
            "demo.process.cpu.utilization",
            observeValues: () => new[] { new Measurement<double>(ObserveCpuPercent(), tags) },
            unit: "%",
            description: "Process CPU usage 0-100 (wall-normalized)");
        m.CreateObservableGauge(
            "demo.process.memory.usage",
            observeValues: () => new[] { new Measurement<long>(ObserveMemoryBytes(), tags) },
            unit: "By",
            description: "Process RSS (working set)");
    }

    private static double ObserveCpuPercent()
    {
        lock (s_cpuGaugeLock)
        {
            s_metricProcess.Refresh();
            var wall = Stopwatch.GetTimestamp();
            var cpu = s_metricProcess.TotalProcessorTime.Ticks;
            if (s_lastCpuGaugeWallTicks == 0)
            {
                s_lastCpuGaugeWallTicks = wall;
                s_lastCpuGaugeCpuTicks = cpu;
                s_lastPublishedCpuPct = 0.0;
                return 0.0;
            }

            var dw = wall - s_lastCpuGaugeWallTicks;
            if (dw <= 0)
                return s_lastPublishedCpuPct;

            // TotalProcessorTime skokowo; krótkie okno => dc=0 i fałszywe 0%. Czekamy na min. ~100 ms ściany.
            var wallSec = dw / (double)Stopwatch.Frequency;
            if (wallSec < 0.1)
                return s_lastPublishedCpuPct;

            var dc = Math.Max(0L, cpu - s_lastCpuGaugeCpuTicks);
            s_lastCpuGaugeWallTicks = wall;
            s_lastCpuGaugeCpuTicks = cpu;
            var cpuSec = dc / (double)TimeSpan.TicksPerSecond;
            // Czas CPU procesu / czas ściany * 100 ≈ "% jednego rdzenia" (1 wątek max ~100); clamp jak w planie.
            var pct = wallSec > 0 ? cpuSec / wallSec * 100.0 : 0.0;
            s_lastPublishedCpuPct = Math.Clamp(pct, 0.0, 100.0);
            return s_lastPublishedCpuPct;
        }
    }

    private static long ObserveMemoryBytes()
    {
        s_metricProcess.Refresh();
        return s_metricProcess.WorkingSet64;
    }

    private static string PeerIdFromEnvTail(string tail)
    {
        var t = tail.Trim().ToLowerInvariant();
        if (t == "py")
            throw new InvalidOperationException(
                "DEMO_PEER_PY removed — use DEMO_PEER_PY_GATEWAY / DEMO_PEER_PY_WORKER (route ids py-gateway / py-worker).");
        if (t.StartsWith("py_", StringComparison.Ordinal))
            return t.Replace('_', '-');
        return t;
    }

    private static Dictionary<string, string> LoadPeerMap()
    {
        var mapJson = Environment.GetEnvironmentVariable("DEMO_PEER_MAP");
        if (!string.IsNullOrWhiteSpace(mapJson))
        {
            var d = JsonSerializer.Deserialize<Dictionary<string, string>>(mapJson)
                    ?? new Dictionary<string, string>();
            var o = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var kv in d)
            {
                var v = kv.Value.Trim();
                if (v.Length > 0)
                    o[PeerIdFromEnvTail(kv.Key.Trim().ToLowerInvariant())] = v;
            }

            return o;
        }

        var m = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry kv in Environment.GetEnvironmentVariables().Cast<DictionaryEntry>())
        {
            var key = kv.Key?.ToString();
            if (string.IsNullOrEmpty(key) || key == "DEMO_PEER_MAP")
                continue;
            const string prefix = "DEMO_PEER_";
            if (!key.StartsWith(prefix, StringComparison.Ordinal))
                continue;
            var tail = key[prefix.Length..].Trim().ToLowerInvariant();
            var val = kv.Value?.ToString()?.Trim();
            if (tail.Length > 0 && !string.IsNullOrEmpty(val))
                m[PeerIdFromEnvTail(tail)] = val!;
        }

        return m;
    }

    private static bool TryParseProcessing(string? s, double cap, out double seconds, out string? err)
    {
        err = null;
        seconds = 0;
        if (string.IsNullOrWhiteSpace(s))
            return true;
        var t = s.Trim();
        var low = t.ToLowerInvariant();
        double raw;
        if (low.EndsWith("ms", StringComparison.Ordinal))
        {
            var n = t[..^2].Trim();
            if (!double.TryParse(n, CultureInfo.InvariantCulture, out raw))
            {
                err = $"invalid processing_time: {s}";
                return false;
            }

            seconds = raw / 1000.0;
        }
        else if (low.EndsWith('s'))
        {
            var n = t[..^1].Trim();
            if (!double.TryParse(n, CultureInfo.InvariantCulture, out raw))
            {
                err = $"invalid processing_time: {s}";
                return false;
            }

            seconds = raw;
        }
        else
        {
            err = $"invalid processing_time: {s} (use e.g. 5.6s or 100ms)";
            return false;
        }

        if (seconds < 0)
        {
            err = "processing_time must be non-negative";
            return false;
        }

        if (seconds > cap)
            seconds = cap;
        return true;
    }

    private static string ActivityNameFromActivity(string act)
    {
        if (string.IsNullOrWhiteSpace(act))
            return "pipeline.processing_step";
        return act.Length <= 256 ? act : act[..256];
    }

    private static string? RouteSegmentSchemaError(JsonObject seg, string where)
    {
        if (seg.ContainsKey("processing_time"))
        {
            return $"{where}: field 'processing_time' is not supported; use non-empty 'processing_steps' with {{\"activity\",\"time\"}}";
        }

        if (seg["processing_steps"] is not JsonArray ps || ps.Count == 0)
            return $"{where}: non-empty 'processing_steps' is required";

        return null;
    }

    private static string? ValidateNestedRoute(JsonArray nested, int stepIndex)
    {
        if (nested.Count == 0)
            return $"processing_steps[{stepIndex}].nested_route must be non-empty";
        var first = nested[0]?.AsObject();
        var fid = first?["id"]?.GetValue<string>() ?? "";
        if (string.IsNullOrWhiteSpace(fid))
            return $"processing_steps[{stepIndex}].nested_route[0].id must be non-empty";
        var fl = fid.Trim().ToLowerInvariant();
        if (fl == "py" || fl == "py-gateway")
            return $"processing_steps[{stepIndex}].nested_route[0].id must not be '{fl}' (workers only)";
        for (var j = 0; j < nested.Count; j++)
        {
            var sjo = nested[j]?.AsObject();
            if (sjo is null)
                return $"processing_steps[{stepIndex}].nested_route[{j}] must be an object";
            var err = RouteSegmentSchemaError(sjo, $"processing_steps[{stepIndex}].nested_route[{j}]");
            if (err is not null)
                return err;
        }

        return null;
    }

    private static JsonArray CloneRouteWithVisitedFalse(JsonArray src)
    {
        var o = new JsonArray();
        foreach (var el in src)
        {
            if (el is not JsonObject jo)
                continue;
            var copy = JsonNode.Parse(jo.ToJsonString()) as JsonObject ?? new JsonObject();
            copy["visited"] = false;
            o.Add(copy);
        }

        return o;
    }

    private static JsonObject BuildNestedPipelineRoot(JsonArray nestedRouteFresh)
    {
        return new JsonObject
        {
            ["route"] = nestedRouteFresh,
            ["visit_log"] = new JsonArray(),
            ["counter"] = "0",
            ["table_of_workers"] = new JsonArray(),
        };
    }

    /// <summary>
    /// Span <c>pipeline.hop</c> z rodzicem z nagłówków W3C (<c>traceparent</c>) — tak samo jak Python/Rust,
    /// bo <see cref="Activity.Current"/> z ASP.NET bywa puste lub bez relacji do upstreamu przy minimalnych API.
    /// </summary>
    private static Activity? StartPipelineHopActivity(PropagationContext incoming)
    {
        var remote = incoming.ActivityContext;
        if (remote.TraceId != default)
            return Act.StartActivity("pipeline.hop", ActivityKind.Server, remote);

        var cur = Activity.Current;
        if (cur is not null)
            return Act.StartActivity("pipeline.hop", ActivityKind.Internal, cur.Context);
        return Act.StartActivity("pipeline.hop", ActivityKind.Server);
    }

    private static IEnumerable<string> TraceHeadersGetter(IHeaderDictionary headers, string name)
    {
        if (!headers.TryGetValue(name, out var values))
            return Array.Empty<string>();
        var s = values.ToString();
        return string.IsNullOrEmpty(s) ? Array.Empty<string>() : new[] { s };
    }

    private static int? FirstUnvisited(JsonArray route)
    {
        for (var i = 0; i < route.Count; i++)
        {
            var seg = route[i]?.AsObject();
            if (seg is null)
                continue;
            var vis = seg["visited"]?.GetValue<bool>() ?? false;
            if (!vis)
                return i;
        }

        return null;
    }

    private static void BumpCounter(JsonObject root, string cid)
    {
        var c0 = int.Parse(root["counter"]?.GetValue<string>() ?? "0", CultureInfo.InvariantCulture);
        var list = new List<string>();
        if (root["table_of_workers"] is JsonArray ta)
        {
            foreach (var e in ta)
            {
                if (e is JsonValue jv && jv.TryGetValue<string>(out var s))
                    list.Add(s);
            }
        }

        root["counter"] = (c0 + 1).ToString(CultureInfo.InvariantCulture);
        var na = new JsonArray();
        foreach (var x in list)
            na.Add(x);
        na.Add(cid);
        root["table_of_workers"] = na;
    }

    public static async Task Main()
    {
        if (GetLo("DEMO_MODE", "pipeline").Equals("exercises", StringComparison.OrdinalIgnoreCase))
        {
            CsLine("DEMO_MODE=exercises: legacy off — użyj DEMO_MODE=pipeline.");
            return;
        }

        // Obraz aspnet ustawia ASPNETCORE_HTTP_PORTS; host musi wczytać ASPNETCORE_URLS przed CreateBuilder,
        // inaczej Kestrel może nie nasłuchiwać na DEMO_HTTP_ADDR (pusty port / zły port względem DEMO_PEER_*).
        var bindUrls = GetLo("DEMO_HTTP_ADDR", "0.0.0.0:8080").Trim();
        if (!bindUrls.StartsWith("http", StringComparison.OrdinalIgnoreCase))
            bindUrls = "http://" + bindUrls;
        Environment.SetEnvironmentVariable("ASPNETCORE_URLS", bindUrls);

        LogPyroscopePushLineIfConfigured();

        // W3C traceparent — inbound Activity + outbound HttpClient używają tego samego co inne workery.
        Sdk.SetDefaultTextMapPropagator(new TraceContextPropagator());

        var builder = WebApplication.CreateBuilder();
        var verboseFw = VerboseFrameworkLogs();
        if (!verboseFw)
        {
            builder.Logging.SetMinimumLevel(LogLevel.Warning);
            builder.Logging.AddFilter("Microsoft", LogLevel.Warning);
            builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
            builder.Logging.AddFilter("Microsoft.AspNetCore.Hosting.Diagnostics", LogLevel.Error);
            builder.Logging.AddFilter("Microsoft.Hosting.Lifetime", LogLevel.Warning);
            builder.Logging.AddFilter("System.Net.Http", LogLevel.Warning);
            builder.Logging.AddFilter("System.Net.Http.HttpClient", LogLevel.Warning);
            builder.Logging.AddFilter("OpenTelemetry", LogLevel.Warning);
            builder.Logging.AddFilter("OpenTelemetry.Exporter", LogLevel.Error);
            builder.Logging.AddFilter(
                (category, _, level) =>
                {
                    if (category is null)
                        return true;
                    if (category.Contains("OtlpTraceExporter", StringComparison.Ordinal)
                        || category.Contains("OtlpMetricExporter", StringComparison.Ordinal)
                        || category.Contains("OtlpLogExporter", StringComparison.Ordinal))
                        return level >= LogLevel.Warning;
                    return true;
                });
        }
        else
        {
            builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
        }

        if (UseOtlp() && UseOtlLogExport())
        {
            builder.Logging.AddOpenTelemetry(logging =>
            {
                logging.SetResourceBuilder(ResB());
                logging.AddOtlpExporter(o =>
                {
                    o.Endpoint = new Uri(LogEp());
                    o.Protocol = OtlpExportProtocol.HttpProtobuf;
                });
            });
        }

        builder.Services.AddHttpClient();

        builder.WebHost.UseUrls(bindUrls);

        var pipelinePath = GetLo("DEMO_HTTP_PATH", "/v1/pipeline");

        if (UseOtlp())
        {
            builder.Services.AddOpenTelemetry()
                .WithTracing(
                    t => t
                        .SetResourceBuilder(ResB())
                        .AddSource(Act.Name)
                        .AddAspNetCoreInstrumentation(o =>
                        {
                            // Filter: true = zbieraj. Dla /v1/pipeline zwracamy false — bez osobnego spanu „POST …”.
                            o.Filter = ctx =>
                            {
                                var req = ctx.Request.Path.Value ?? "";
                                var isPipeline = req.Equals(pipelinePath, StringComparison.OrdinalIgnoreCase)
                                                 || req.TrimEnd('/').Equals(
                                                     pipelinePath.TrimEnd('/'),
                                                     StringComparison.OrdinalIgnoreCase);
                                return !isPipeline;
                            };
                        })
                        .AddHttpClientInstrumentation()
                        .AddOtlpExporter(
                            o =>
                            {
                                o.Endpoint = new Uri(TrEp());
                                o.Protocol = OtlpExportProtocol.HttpProtobuf;
                            }))
                .WithMetrics(
                    m => m
                        .SetResourceBuilder(ResB())
                        .AddMeter("worker_csharp")
                        .AddOtlpExporter(
                            (metricExporterOptions, metricReaderOptions) =>
                            {
                                metricExporterOptions.Endpoint = new Uri(MxEp());
                                metricExporterOptions.Protocol = OtlpExportProtocol.HttpProtobuf;
                                metricReaderOptions.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds =
                                    5000;
                            }));
        }
        else
        {
            builder.Services.AddOpenTelemetry()
                .WithTracing(
                    tt => tt
                        .SetResourceBuilder(ResB())
                        .AddSource(Act.Name)
                        .AddAspNetCoreInstrumentation(o =>
                        {
                            o.Filter = ctx =>
                            {
                                var req = ctx.Request.Path.Value ?? "";
                                var isPipeline = req.Equals(pipelinePath, StringComparison.OrdinalIgnoreCase)
                                                 || req.TrimEnd('/').Equals(
                                                     pipelinePath.TrimEnd('/'),
                                                     StringComparison.OrdinalIgnoreCase);
                                return !isPipeline;
                            };
                        })
                        .AddHttpClientInstrumentation());
        }

        var app = builder.Build();
        s_pipelineLog = app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("demo.pipeline");
        var path = pipelinePath;
        var cid = GetLo("DEMO_WORKER_ID", "cs");
        var peers = LoadPeerMap();
        var maxProc = MaxProcSec();

        var m = new Meter("worker_csharp", "1.0.0");
        var msgCount = m.CreateCounter<long>("demo.pipeline.messages", description: "messages processed in node");
        var hopDuration = m.CreateHistogram<double>("demo.pipeline.hop.duration_ms", description: "hop time ms", unit: "ms");

        var otelSvc = Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME")?.Trim();
        var svcMetrics = string.IsNullOrEmpty(otelSvc) ? "worker_csharp" : otelSvc;
        if (UseOtlp() && ProcessMetricsEnabled())
            RegisterProcessMetrics(m, svcMetrics, cid);

        app.Lifetime.ApplicationStarted.Register(
            () => CsLine($"C# pipeline {bindUrls}{path} worker_id={cid} peers=[{string.Join(",", peers.Keys)}]"));

        app.MapPost(
            path,
            async (HttpContext ctx, IHttpClientFactory httpFactory) =>
            {
                ctx.Request.EnableBuffering();
                var incomingTrace = Propagators.DefaultTextMapPropagator.Extract(
                    default,
                    ctx.Request.Headers,
                    TraceHeadersGetter);
                string text;
                using (var r = new StreamReader(ctx.Request.Body, Encoding.UTF8, leaveOpen: true))
                {
                    text = await r.ReadToEndAsync();
                }

                JsonNode? rootNode;
                try
                {
                    rootNode = JsonNode.Parse(string.IsNullOrEmpty(text) ? "{}" : text);
                }
                catch
                {
                    return Results.BadRequest("Invalid JSON");
                }

                if (rootNode is not JsonObject rootObj)
                    return Results.BadRequest("JSON must be an object");

                CsLine($"[{cid}] received: {(string.IsNullOrEmpty(text) ? "{}" : text)}");

                if (rootObj["route"] is JsonArray routeArr && routeArr.Count > 0)
                {
                    var routeRes = await HandleRouteAsync(
                        rootObj,
                        routeArr,
                        cid,
                        peers,
                        maxProc,
                        httpFactory,
                        hopDuration,
                        msgCount,
                        incomingTrace,
                        svcMetrics);
                    return routeRes;
                }

                return Results.BadRequest("JSON must include non-empty route array");
            });

        await app.RunAsync();
    }

    private static async Task<IResult> HandleRouteAsync(
        JsonObject root,
        JsonArray route,
        string cid,
        Dictionary<string, string> peers,
        double maxProc,
        IHttpClientFactory httpFactory,
        Histogram<double> hopDuration,
        Counter<long> msgCount,
        PropagationContext incomingTrace,
        string serviceNameForMetrics)
    {
        var t0 = Stopwatch.GetTimestamp();
        var pipelineTags = new TagList
        {
            { "service.name", serviceNameForMetrics },
            { "demo.worker_id", cid },
        };
        using var hopAct = StartPipelineHopActivity(incomingTrace);

        var fi = FirstUnvisited(route);
        if (fi is null)
            return Results.BadRequest("route has no unvisited segment");

        var seg = route[fi.Value]?.AsObject();
        if (seg is null)
            return Results.BadRequest("invalid route segment");

        var segId = seg["id"]?.GetValue<string>();
        if (segId != cid)
        {
            return Results.BadRequest(
                $"first unvisited route segment id must match this node (expected {cid}, got {segId})");
        }

        var topSchema = RouteSegmentSchemaError(seg, "route segment");
        if (topSchema is not null)
            return Results.BadRequest(topSchema);

        var stepsArr = (JsonArray)seg["processing_steps"]!;
        var parsed = new List<(string Act, double Spin, JsonArray? Nested)>();
        for (var i = 0; i < stepsArr.Count; i++)
        {
            var o = stepsArr[i]?.AsObject();
            if (o is null)
                return Results.BadRequest($"processing_steps[{i}] must be an object");
            var act = o["activity"]?.GetValue<string>() ?? $"step_{i}";
            if (o["nested_route"] is JsonArray nr && nr.Count > 0)
            {
                var verr = ValidateNestedRoute(nr, i);
                if (verr is not null)
                    return Results.BadRequest(verr);
                if (!TryParseProcessing(o["time"]?.GetValue<string>(), maxProc, out var pre, out var perr2))
                    return Results.BadRequest(perr2 ?? $"processing_steps[{i}].time invalid");
                parsed.Add((act, pre, nr));
            }
            else
            {
                if (!TryParseProcessing(o["time"]?.GetValue<string>(), maxProc, out var sec, out var perr))
                    return Results.BadRequest(perr ?? $"processing_steps[{i}].time invalid");
                parsed.Add((act, sec, null));
            }
        }

        var cpuSum = parsed.Sum(x => x.Spin);
        var hasNested = parsed.Any(x => x.Nested is not null);
        hopAct?.SetTag("demo.simulated_processing_sec", cpuSum);
        hopAct?.SetTag("demo.processing.mode", "steps");
        hopAct?.SetTag("demo.step_count", parsed.Count);
        hopAct?.SetTag("demo.has_nested_steps", hasNested);
        using (Act.StartActivity("pipeline.processing", ActivityKind.Internal))
        {
            for (var i = 0; i < parsed.Count; i++)
            {
                var (act, spin, nested) = parsed[i];
                var name = ActivityNameFromActivity(act);
                using (Act.StartActivity(name, ActivityKind.Internal))
                {
                    Activity.Current?.SetTag("demo.activity", act);
                    Activity.Current?.SetTag("demo.cpu_spin_sec", spin);
                    Activity.Current?.SetTag("demo.step_index", i);
                    if (nested is not null)
                        Activity.Current?.SetTag("demo.nested_subpipeline", true);
                    if (spin > 0)
                        await Task.Run(() => CpuSpinForSeconds(spin));
                    if (nested is not null)
                    {
                        var clone = CloneRouteWithVisitedFalse(nested);
                        var rootNest = BuildNestedPipelineRoot(clone);
                        var firstSeg = nested[0]?.AsObject();
                        var firstId = firstSeg?["id"]?.GetValue<string>()?.Trim();
                        if (string.IsNullOrWhiteSpace(firstId))
                            return Results.BadRequest($"processing_steps[{i}].nested_route[0].id empty");
                        var nestedPeerKey = firstId.Trim().ToLowerInvariant();
                        if (!peers.TryGetValue(nestedPeerKey, out var nurl))
                        {
                            return Results.Json(
                                new { error = $"no peer URL for nested id {firstId}" },
                                statusCode: 502);
                        }

                        var nestJson = NodeToJsonString(rootNest);
                        CsLine($"[{cid}] nested_forward to {nurl}: {nestJson}");
                        using (Act.StartActivity("pipeline.nested_forward", ActivityKind.Client))
                        {
                            Activity.Current?.SetTag("http.url", nurl);
                            var client = httpFactory.CreateClient();
                            using var req = new HttpRequestMessage(HttpMethod.Post, nurl)
                            {
                                Content = new StringContent(nestJson, Encoding.UTF8, "application/json"),
                            };
                            req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
                            var resp = await client.SendAsync(req);
                            Activity.Current?.SetTag("demo.downstream_status", (int)resp.StatusCode);
                            if (!resp.IsSuccessStatusCode)
                            {
                                return Results.Json(
                                    new { error = $"nested_forward HTTP {(int)resp.StatusCode}" },
                                    statusCode: 502);
                            }
                        }
                    }
                }
            }
        }

        seg["visited"] = true;
        if (root["visit_log"] is not JsonArray vl)
        {
            vl = new JsonArray();
            root["visit_log"] = vl;
        }

        vl.Add(cid);
        BumpCounter(root, cid);

        var nextIdx = FirstUnvisited(route);
        var outJson = NodeToJsonString(root);

        if (nextIdx is null)
        {
            CsLine($"[{cid}] respond (terminal route): {outJson}");
            hopDuration.Record(Stopwatch.GetElapsedTime(t0).TotalMilliseconds, pipelineTags);
            msgCount.Add(1, pipelineTags);
            return Results.Content(outJson, "application/json", statusCode: 200);
        }

        var nextSeg = route[nextIdx.Value]?.AsObject();
        var nextId = nextSeg?["id"]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(nextId))
            return Results.BadRequest("invalid next route segment id");

        var nk = nextId.Trim().ToLowerInvariant();
        if (!peers.TryGetValue(nk, out var url))
        {
            return Results.Json(
                new { error = $"no peer URL for id {nextId}" },
                statusCode: 502);
        }

        CsLine($"[{cid}] forward to {url}: {outJson}");

        using (Act.StartActivity("pipeline.forward", kind: ActivityKind.Client))
        {
            var client = httpFactory.CreateClient();
            using var req = new HttpRequestMessage(HttpMethod.Post, url)
            {
                Content = new StringContent(outJson, Encoding.UTF8, "application/json"),
            };
            req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            var resp = await client.SendAsync(req);
            var txt = await resp.Content.ReadAsStringAsync();
            hopDuration.Record(Stopwatch.GetElapsedTime(t0).TotalMilliseconds, pipelineTags);
            msgCount.Add(1, pipelineTags);
            return Results.Content(txt, "application/json", statusCode: (int)resp.StatusCode);
        }
    }
}
