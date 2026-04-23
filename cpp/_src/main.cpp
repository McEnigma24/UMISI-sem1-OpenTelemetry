#include "__preprocessor__.h"

#include <opentelemetry/context/context.h>
#include <opentelemetry/exporters/ostream/span_exporter_factory.h>
#include <opentelemetry/exporters/otlp/otlp_http.h>
#include <opentelemetry/exporters/otlp/otlp_http_exporter_factory.h>
#include <opentelemetry/exporters/otlp/otlp_http_exporter_options.h>
#include <opentelemetry/nostd/shared_ptr.h>
#include <opentelemetry/sdk/resource/resource.h>
#include <opentelemetry/sdk/trace/processor.h>
#include <opentelemetry/sdk/trace/provider.h>
#include <opentelemetry/sdk/trace/simple_processor_factory.h>
#include <opentelemetry/sdk/trace/tracer_provider.h>
#include <opentelemetry/sdk/trace/tracer_provider_factory.h>
#include <opentelemetry/trace/provider.h>
#include <opentelemetry/trace/span_metadata.h>
#include <opentelemetry/trace/tracer.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>

namespace
{

namespace trace_api = opentelemetry::trace;
namespace trace_sdk = opentelemetry::sdk::trace;
namespace trace_exp = opentelemetry::exporter::trace;
namespace otlp      = opentelemetry::exporter::otlp;

using opentelemetry::context::Context;
using opentelemetry::trace::kIsRootSpanKey;
using opentelemetry::trace::SpanKind;
using opentelemetry::trace::StartSpanOptions;
using opentelemetry::trace::StatusCode;
using opentelemetry::trace::Tracer;

/**
 * false = ostream (stderr) — domyślnie, jak wcześniej.
 * true  = OTLP/HTTP (JSON); URL: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT / spec OTLP.
 */
bool use_otlp_http_export()
{
    const char* m = std::getenv("OTEL_DEMO_TRACE_EXPORT");
    if (!m || m[0] == '\0')
    {
        return false;
    }
    if (std::strcmp(m, "ostream") == 0)
    {
        return false;
    }
    if (std::strcmp(m, "otlp") == 0 || std::strcmp(m, "http") == 0)
    {
        return true;
    }
    return false;
}

std::shared_ptr<trace_sdk::TracerProvider> g_sdk_tracer_provider;

/** SDK + OStreamSpanExporter — spany na stderr (ten sam proces, czytelny zrzut). */
void init_tracer_export_to_cerr()
{
    auto exporter  = trace_exp::OStreamSpanExporterFactory::Create(std::cerr);
    auto processor = trace_sdk::SimpleSpanProcessorFactory::Create(std::move(exporter));

    // można użyć BatchSpanProcessor //

    const auto resource =
        opentelemetry::sdk::resource::Resource::Create({{"service.name", "demo_app"},
                                                        {"service.version", "1.0.0"}});
    auto up = trace_sdk::TracerProviderFactory::Create(std::move(processor), resource);
    g_sdk_tracer_provider = std::shared_ptr<trace_sdk::TracerProvider>(std::move(up));
    std::shared_ptr<trace_api::TracerProvider> api = g_sdk_tracer_provider;
    opentelemetry::nostd::shared_ptr<trace_api::TracerProvider> n{api};
    trace_sdk::Provider::SetTracerProvider(n);
}

/**
 * OTLP/HTTP (JSON). Przy gołym netcat bez odpowiedzi HTTP klient czeka do timeoutu — skracamy timeout.
 */
void init_tracer_otlp_http()
{
    otlp::OtlpHttpExporterOptions opts;
    opts.content_type = otlp::HttpRequestContentType::kJson;
    opts.timeout      = std::chrono::seconds(5); // blocking -> further execution is blocked until we receive RESP or Timeout Expires
    auto exporter     = otlp::OtlpHttpExporterFactory::Create(opts);
    auto processor    = trace_sdk::SimpleSpanProcessorFactory::Create(std::move(exporter));

    // można użyć BatchSpanProcessor //  -> tam można nazbierać kolejkę -> batch przerzucić do wysłania na osobny wątek
    // główny zbiera wtedy kolejne logi -> zapiełni się to odpala następny wątek .itd

    const auto resource = opentelemetry::sdk::resource::Resource::Create({{"service.name", "demo_app"}, {"service.version", "1.0.0"}});
    auto up = trace_sdk::TracerProviderFactory::Create(std::move(processor), resource);
    g_sdk_tracer_provider = std::shared_ptr<trace_sdk::TracerProvider>(std::move(up));
    std::shared_ptr<trace_api::TracerProvider> api = g_sdk_tracer_provider;
    opentelemetry::nostd::shared_ptr<trace_api::TracerProvider> n{api};
    trace_sdk::Provider::SetTracerProvider(n);
}

void shutdown_tracer()
{
    if (g_sdk_tracer_provider)
    {
        g_sdk_tracer_provider->ForceFlush();
    }
    g_sdk_tracer_provider.reset();
    std::shared_ptr<trace_api::TracerProvider> none;
    opentelemetry::nostd::shared_ptr<trace_api::TracerProvider> n{none};
    trace_sdk::Provider::SetTracerProvider(n);
}

void opentelemetry_api_exercises(const opentelemetry::nostd::shared_ptr<Tracer> &tracer)
{
    // ---- 1) Nested spans + active context (implicit parent) ----
    {
        auto outer   = tracer->StartSpan("Outer operation");
        auto s_outer = Tracer::WithActiveSpan(outer);
        {
            auto inner   = tracer->StartSpan("Inner operation");
            auto s_inner = Tracer::WithActiveSpan(inner);
            const bool current_matches = (Tracer::GetCurrentSpan() == inner);
            line("OpenTelemetry [1/6]: nested spans; GetCurrentSpan == inner span");
            if (!current_matches)
            {
                line("OpenTelemetry [1/6]: note — GetCurrentSpan not equal to inner (check impl.)");
            }
            inner->End();
        }
        outer->End();
    }

    // ---- 2) StartSpan with attributes at creation + SetAttribute + events ----
    {
        auto span = tracer->StartSpan(
            "enriched",
            {{"http.method", "GET"}, {"http.status_code", static_cast<int64_t>(200)}},
            [] {
                StartSpanOptions o;
                o.kind = SpanKind::kClient;
                return o;
            }());
        auto scope = Tracer::WithActiveSpan(span);

        span->SetAttribute("work.units", 42.0);
        span->SetAttribute("flag.ok", true);
        span->AddEvent("checkpoint");
        span->AddEvent("with_attrs", {{"step", "after_io"}, {"rc", static_cast<int64_t>(0)}});

        line("OpenTelemetry [2/6]: attributes, events, SpanKind::kClient (export: stderr albo OTLP/HTTP)");
        span->SetStatus(StatusCode::kOk);
        span->End();
    }

    // ---- 3) UpdateName + error status ----
    {
        auto span  = tracer->StartSpan("original_name");
        auto scope = Tracer::WithActiveSpan(span);
        span->UpdateName("renamed_op");
        span->SetStatus(StatusCode::kError, "synthetic failure for status path");
        line("OpenTelemetry [3/6]: UpdateName, SetStatus(Error)");
        span->End();
    }

    // ---- 4) Explicit root span (no parent in context) ----
    {
        Context root = Context{}.SetValue(kIsRootSpanKey, true);
        StartSpanOptions opt;
        opt.parent = root;
        auto root_span  = tracer->StartSpan("explicit_root", opt);
        auto root_scope = Tracer::WithActiveSpan(root_span);
        line("OpenTelemetry [4/6]: root span via Context + kIsRootSpanKey");
        root_span->SetStatus(StatusCode::kOk);
        root_span->End();
    }

    // ---- 5) IsRecording ----
    {
        auto s = tracer->StartSpan("recording_probe");
        const bool rec = s->IsRecording();
        s->SetStatus(StatusCode::kOk);
        s->End();
        if (rec)
        {
            line("OpenTelemetry [5/6]: IsRecording() == true (SDK span)");
        }
        else
        {
            line("OpenTelemetry [5/6]: IsRecording() == false (unexpected with SDK — check config)");
        }
    }

    // ---- 6) SpanContext validity ----
    {
        auto s = tracer->StartSpan("context_probe");
        const bool valid = s->GetContext().IsValid();
        s->SetStatus(StatusCode::kOk);
        s->End();
        if (valid)
        {
            line("OpenTelemetry [6/6]: GetContext().IsValid() == true");
        }
        else
        {
            line("OpenTelemetry [6/6]: GetContext().IsValid() == false (unexpected with SDK)");
        }
    }
}

}  // namespace

#ifdef BUILD_EXECUTABLE
int main(int argc, char* argv[])
{
    srand(time(NULL));
    CORE::clear_terminal();
    time_stamp("It just works");

    // const bool otlp = use_otlp_http_export();   //  exported variable determines

    const bool otlp = true;
    if (otlp)
    {
        line("OTEL_DEMO_TRACE_EXPORT=otlp: eksport HTTP (nc: ./scripts/listen_otlp_http.sh ; endpoint: "
             "OTEL_EXPORTER_OTLP_*)");
        init_tracer_otlp_http();
    }
    else
    {
        line("Domyślnie ostream/stderr. OTLP/HTTP:  export OTEL_DEMO_TRACE_EXPORT=otlp  potem start.sh");
        init_tracer_export_to_cerr();
    }

    const auto provider = trace_api::Provider::GetTracerProvider();
    const auto tracer   = provider->GetTracer("demo_app", "1.0.0");
    opentelemetry_api_exercises(tracer);

    line("OpenTelemetry: zakończone (stderr vs OTLP — patrz OTEL_DEMO_TRACE_EXPORT).");

    shutdown_tracer();
    return 0;
}
#endif
