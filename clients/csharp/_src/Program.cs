/*
 * HTTP pipeline (C# — węzeł terminalny). POST DEMO_HTTP_PATH, JSON.
 */
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Net;
using System.Text;
using System.Text.Json;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace OtelDemo;

public static class Program
{
    private static readonly ActivitySource Act = new("demo_app", "1.0.0");
    private static void Line(string m) => Console.WriteLine(m);
    private static string GetLo(string k, string d) => Environment.GetEnvironmentVariable(k) ?? d;

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

    public static async Task Main()
    {
        if (GetLo("DEMO_MODE", "pipeline").Equals("exercises", StringComparison.OrdinalIgnoreCase))
        {
            Line("DEMO_MODE=exercises: legacy off — użyj DEMO_MODE=pipeline.");
            return;
        }

        var builder = WebApplication.CreateBuilder();
        var l = GetLo("DEMO_HTTP_ADDR", "0.0.0.0:8080");
        if (!l.StartsWith("http", StringComparison.OrdinalIgnoreCase))
        {
            l = "http://" + l;
        }

        builder.WebHost.UseUrls(l);

        if (UseOtlp())
        {
            builder.Services.AddOpenTelemetry()
                .WithTracing(
                    t => t
                        .SetResourceBuilder(ResB())
                        .AddSource(Act.Name)
                        .AddAspNetCoreInstrumentation()
                        .AddOtlpExporter(
                            o =>
                            {
                                o.Endpoint = new Uri(TrEp());
                                o.Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf;
                            }))
                .WithMetrics(
                    m => m
                        .SetResourceBuilder(ResB())
                        .AddMeter("demo_app")
                        .AddOtlpExporter(
                            o =>
                            {
                                o.Endpoint = new Uri(MxEp());
                                o.Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf;
                            }));
        }
        else
        {
            builder.Services.AddOpenTelemetry()
                .WithTracing(
                    t => t
                        .SetResourceBuilder(ResB())
                        .AddSource(Act.Name)
                        .AddAspNetCoreInstrumentation());
        }

        var app = builder.Build();
        var path = GetLo("DEMO_HTTP_PATH", "/v1/pipeline");
        var cid = GetLo("DEMO_CLIENT_ID", "cs");

        var m = new Meter("demo_app", "1.0.0");
        var msgCount = m.CreateCounter<long>("demo.pipeline.messages", description: "messages processed in node");
        var hopDuration = m.CreateHistogram<double>("demo.pipeline.hop.duration_ms", description: "hop time ms", unit: "ms");

        app.Lifetime.ApplicationStarted.Register(
            () => Line($"C# pipeline {l}{path} client_id={cid} (terminal)"));

        app.MapPost(
            path,
            async (HttpContext ctx) =>
            {
                var t0 = Stopwatch.GetTimestamp();
                using (Act.StartActivity("pipeline.hop", kind: ActivityKind.Server))
                {
                    ctx.Request.EnableBuffering();
                    string text;
                    using (var r = new StreamReader(ctx.Request.Body, Encoding.UTF8, leaveOpen: true))
                    {
                        text = await r.ReadToEndAsync();
                    }

                    JsonDocument jdoc;
                    try
                    {
                        jdoc = JsonDocument.Parse(
                            string.IsNullOrEmpty(text) ? "{}" : text);
                    }
                    catch
                    {
                        return Results.BadRequest("Invalid JSON");
                    }

                    if (jdoc.RootElement.ValueKind != JsonValueKind.Object)
                    {
                        return Results.BadRequest("JSON must be an object");
                    }

                    var root = jdoc.RootElement;
                    var c0 = int.Parse(root.GetProperty("counter").GetString() ?? "0");
                    var list = new List<string>();
                    if (root.TryGetProperty("table_of_clients", out var tc)
                        && tc.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var e in tc.EnumerateArray())
                        {
                            if (e.ValueKind == JsonValueKind.String)
                            {
                                list.Add(e.GetString()!);
                            }
                        }
                    }

                    c0++;
                    list.Add(cid);
                    var outJ = new
                    {
                        counter = c0.ToString(),
                        table_of_clients = list,
                    };

                    hopDuration.Record(Stopwatch.GetElapsedTime(t0).TotalMilliseconds);
                    msgCount.Add(1);
                    return Results.Json(outJ);
                }
            });

        await app.RunAsync();
    }
}
