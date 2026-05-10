/*
 * HTTP pipeline (C#). POST DEMO_HTTP_PATH — JSON z ``route``.
 *
 * DEMO_VERBOSE_FRAMEWORK_LOGS — true/1/yes: więcej Microsoft/System.Net.Http/OpenTelemetry;
 *   domyślnie (false) — Warning+ dla frameworka i wyciszenie OTLP HttpClient (info).
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
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace OtelDemo;

public static class Program
{
    private static readonly ActivitySource Act = new("demo_app", "1.0.0");
    private static readonly JsonSerializerOptions s_pipelineJson = new()
    {
        WriteIndented = false,
        TypeInfoResolver = new DefaultJsonTypeInfoResolver(),
    };

    private static void CsLine(string m) => Console.WriteLine($"{DateTime.Now:HH:mm:ss.fff} {m}");

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
            .AddService("demo_app", "1.0.0", autoGenerateServiceInstanceId: false)
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
    private static string TrEp() => GetLo("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces");
    private static string MxEp() => GetLo("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", TrEp().Replace("/v1/traces", "/v1/metrics", StringComparison.Ordinal));

    private static double MaxProcSec() =>
        double.TryParse(GetLo("DEMO_MAX_PROCESSING_SEC", "120"), CultureInfo.InvariantCulture, out var x)
            ? x
            : 120.0;

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
                    o[kv.Key.Trim().ToLowerInvariant()] = v;
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
                m[tail] = val!;
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
        if (root["table_of_clients"] is JsonArray ta)
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
        root["table_of_clients"] = na;
    }

    public static async Task Main()
    {
        if (GetLo("DEMO_MODE", "pipeline").Equals("exercises", StringComparison.OrdinalIgnoreCase))
        {
            CsLine("DEMO_MODE=exercises: legacy off — użyj DEMO_MODE=pipeline.");
            return;
        }

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
                        || category.Contains("OtlpMetricExporter", StringComparison.Ordinal))
                        return level >= LogLevel.Warning;
                    return true;
                });
        }
        else
        {
            builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
        }

        builder.Services.AddHttpClient();

        var l = GetLo("DEMO_HTTP_ADDR", "0.0.0.0:8080");
        if (!l.StartsWith("http", StringComparison.OrdinalIgnoreCase))
            l = "http://" + l;

        builder.WebHost.UseUrls(l);

        if (UseOtlp())
        {
            builder.Services.AddOpenTelemetry()
                .WithTracing(
                    t => t
                        .SetResourceBuilder(ResB())
                        .AddSource(Act.Name)
                        .AddAspNetCoreInstrumentation()
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
                        .AddMeter("demo_app")
                        .AddOtlpExporter(
                            o =>
                            {
                                o.Endpoint = new Uri(MxEp());
                                o.Protocol = OtlpExportProtocol.HttpProtobuf;
                            }));
        }
        else
        {
            builder.Services.AddOpenTelemetry()
                .WithTracing(
                    tt => tt
                        .SetResourceBuilder(ResB())
                        .AddSource(Act.Name)
                        .AddAspNetCoreInstrumentation()
                        .AddHttpClientInstrumentation());
        }

        var app = builder.Build();
        var path = GetLo("DEMO_HTTP_PATH", "/v1/pipeline");
        var cid = GetLo("DEMO_CLIENT_ID", "cs");
        var peers = LoadPeerMap();
        var maxProc = MaxProcSec();

        var m = new Meter("demo_app", "1.0.0");
        var msgCount = m.CreateCounter<long>("demo.pipeline.messages", description: "messages processed in node");
        var hopDuration = m.CreateHistogram<double>("demo.pipeline.hop.duration_ms", description: "hop time ms", unit: "ms");

        app.Lifetime.ApplicationStarted.Register(
            () => CsLine($"C# pipeline {l}{path} client_id={cid} peers=[{string.Join(",", peers.Keys)}]"));

        app.MapPost(
            path,
            async (HttpContext ctx, IHttpClientFactory httpFactory) =>
            {
                ctx.Request.EnableBuffering();
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
                        msgCount);
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
        Counter<long> msgCount)
    {
        var t0 = Stopwatch.GetTimestamp();
        using var hopAct = Act.StartActivity("pipeline.hop", kind: ActivityKind.Server);

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

        var pt = seg["processing_time"]?.GetValue<string>();
        if (!TryParseProcessing(pt, maxProc, out var sleepSec, out var perr))
            return Results.BadRequest(perr ?? "invalid processing_time");

        if (sleepSec > 0)
        {
            using (Act.StartActivity("pipeline.simulated_work", kind: ActivityKind.Internal))
            {
                hopAct?.SetTag("demo.simulated_processing_sec", sleepSec);
                await Task.Delay(TimeSpan.FromSeconds(sleepSec));
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
            hopDuration.Record(Stopwatch.GetElapsedTime(t0).TotalMilliseconds);
            msgCount.Add(1);
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
            hopDuration.Record(Stopwatch.GetElapsedTime(t0).TotalMilliseconds);
            msgCount.Add(1);
            return Results.Content(txt, "application/json", statusCode: (int)resp.StatusCode);
        }
    }
}
