#include "__preprocessor__.h"

#include <opentelemetry/trace/provider.h>

#ifdef BUILD_EXECUTABLE
int main(int argc, char* argv[])
{
    srand(time(NULL));
    // CORE::clear_terminal(); // tests will NOT be VISIBLE with this line
    time_stamp("It just works");

    // OpenTelemetry C++ API (no-op tracer without SDK) — see:
    // https://opentelemetry-cpp.readthedocs.io/en/latest/api/GettingStarted.html
    auto provider   = opentelemetry::trace::Provider::GetTracerProvider();
    auto tracer     = provider->GetTracer("demo_app", "1.0.0");
    auto outer_span = tracer->StartSpan("Outer operation");
    auto outer_scope = tracer->WithActiveSpan(outer_span);
    {
        auto inner_span  = tracer->StartSpan("Inner operation");
        auto inner_scope = tracer->WithActiveSpan(inner_span);
        line("OpenTelemetry: nested spans created (API-only / no-op export)");
        inner_span->End();
    }
    outer_span->End();

    return 0;
}
#endif