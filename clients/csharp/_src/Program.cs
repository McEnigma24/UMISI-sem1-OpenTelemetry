/*
 * Prosty klient OpenTelemetry (OTLP/HTTP protobuf albo console) — analog
 * clients/python/_src/main.py / clients/rust/_src/main.rs.
 * Endpoint: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (domyślnie http://127.0.0.1:4318/v1/traces).
 * Tryb: OTEL_DEMO_TRACE_EXPORT — puste / otlp / http → OTLP; ostream → console; inne (jak Python) → console.
 */
using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace OtelDemo;

internal static class Program
{
    private static void Line(string msg) => Console.WriteLine(msg);

    private static bool UseOtlpHttp()
    {
        var m = Environment.GetEnvironmentVariable("OTEL_DEMO_TRACE_EXPORT") ?? "";
        return m switch
        {
            "" => true,
            "ostream" => false,
            "otlp" or "http" => true,
            _ => false,
        };
    }

    private static TracerProvider BuildTracerProviderOtlpHttp()
    {
        var endpoint = Environment.GetEnvironmentVariable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
                       ?? "http://127.0.0.1:4318/v1/traces";

        var exporterOptions = new OtlpExporterOptions
        {
            Endpoint = new Uri(endpoint),
            Protocol = OtlpExportProtocol.HttpProtobuf,
            TimeoutMilliseconds = 5000,
        };
        var exporter = new OtlpTraceExporter(exporterOptions);

        return Sdk.CreateTracerProviderBuilder()
            .SetResourceBuilder(
                ResourceBuilder.CreateDefault().AddService("demo_app", serviceVersion: "1.0.0"))
            .AddSource("demo_app")
            .AddProcessor(new SimpleActivityExportProcessor(exporter))
            .Build();
    }

    private static TracerProvider BuildTracerProviderConsole()
    {
        var exporter = new ConsoleActivityExporter(new ConsoleExporterOptions
        {
            Targets = ConsoleExporterOutputTargets.Console,
        });

        return Sdk.CreateTracerProviderBuilder()
            .SetResourceBuilder(
                ResourceBuilder.CreateDefault().AddService("demo_app", serviceVersion: "1.0.0"))
            .AddSource("demo_app")
            .AddProcessor(new SimpleActivityExportProcessor(exporter))
            .Build();
    }

    private static void OtelApiExercises(ActivitySource src)
    {
        using (src.StartActivity("Outer operation"))
        {
            using var inner = src.StartActivity("Inner operation");
            var current = Activity.Current;
            var currentMatches = ReferenceEquals(current, inner);
            Line("OpenTelemetry [1/6]: nested spans; GetCurrentSpan == inner span");
            if (!currentMatches)
            {
                Line("OpenTelemetry [1/6]: note — GetCurrentSpan not equal to inner (check impl.)");
            }
        }

        using (var activity = src.StartActivity("enriched", ActivityKind.Client))
        {
            if (activity is not null)
            {
                activity.SetTag("http.method", "GET");
                activity.SetTag("http.status_code", 200);
                activity.SetTag("work.units", 42.0);
                activity.SetTag("flag.ok", true);
                activity.AddEvent(new ActivityEvent("checkpoint"));
                activity.AddEvent(
                    new ActivityEvent(
                        "with_attrs",
                        tags: new ActivityTagsCollection
                        {
                            { "step", "after_io" },
                            { "rc", 0 },
                        }));
                Line("OpenTelemetry [2/6]: attributes, events, SpanKind::CLIENT (export: stderr albo OTLP/HTTP)");
                activity.SetStatus(ActivityStatusCode.Ok);
            }
        }

        using (var activity = src.StartActivity("original_name"))
        {
            if (activity is not null)
            {
                activity.DisplayName = "renamed_op";
                activity.SetStatus(ActivityStatusCode.Error, "synthetic failure for status path");
            }
        }

        Line("OpenTelemetry [3/6]: UpdateName, SetStatus(Error)");

        Activity.Current = null;
        using (var activity = src.StartActivity("explicit_root"))
        {
            Line("OpenTelemetry [4/6]: root span (Activity.Current=null przed start = brak parent trace)");
            activity?.SetStatus(ActivityStatusCode.Ok);
        }

        bool rec;
        using (var activity = src.StartActivity("recording_probe"))
        {
            rec = activity?.IsAllDataRequested ?? false;
            activity?.SetStatus(ActivityStatusCode.Ok);
        }

        if (rec)
        {
            Line("OpenTelemetry [5/6]: IsRecording() == true (SDK span)");
        }
        else
        {
            Line("OpenTelemetry [5/6]: IsRecording() == false (unexpected with SDK — check config)");
        }

        bool valid;
        using (var activity = src.StartActivity("context_probe"))
        {
            valid = activity?.Context.IsValid() ?? false;
            activity?.SetStatus(ActivityStatusCode.Ok);
        }

        if (valid)
        {
            Line("OpenTelemetry [6/6]: GetContext().IsValid() == true");
        }
        else
        {
            Line("OpenTelemetry [6/6]: GetContext().IsValid() == false (unexpected with SDK)");
        }
    }

    private static int Main(string[] args)
    {
        Line($"It just works (C# client, t={DateTime.Now:yyyy-MM-dd HH:mm:ss})");

        TracerProvider provider;
        if (UseOtlpHttp())
        {
            Line("OTEL_DEMO_TRACE_EXPORT=otlp: eksport HTTP (endpoint: OTEL_EXPORTER_OTLP_*)");
            provider = BuildTracerProviderOtlpHttp();
        }
        else
        {
            Line("Domyślnie ostream/console. OTLP/HTTP: export OTEL_DEMO_TRACE_EXPORT=otlp");
            provider = BuildTracerProviderConsole();
        }

        using (provider)
        using (var activitySource = new ActivitySource("demo_app", "1.0.0"))
        {
            Line("C# client — simple demo (args + sleep)");
            if (args.Length == 0)
            {
                Line("No arguments. Try: dotnet run -- hello world");
            }
            else
            {
                Line($"Arguments: {string.Join(' ', args)}");
            }

            OtelApiExercises(activitySource);

            var start = Stopwatch.GetTimestamp();
            Thread.Sleep(50);
            Line($"Elapsed: {Stopwatch.GetElapsedTime(start)}");

            Line("OpenTelemetry: zakończone (console vs OTLP — patrz OTEL_DEMO_TRACE_EXPORT).");
            provider.ForceFlush();
        }

        return 0;
    }
}
