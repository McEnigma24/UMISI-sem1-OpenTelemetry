#!/usr/bin/env python3
"""
Serwer OpenTelemetry: odbieranie OTLP/HTTP (traces) i wypisywanie na stdout.

Dopasowany do tego, co wysyła std. eksporter Python/C++: POST na
/v1/traces z typem application/x-protobuf lub application/json (OTLP/HTTP).
Port domyślnie 4318 (jak spec OTLP) — nadpiszesz: OTEL_RECEIVER_HTTP_PORT,
host: OTEL_RECEIVER_HTTP_HOST (domyślnie 0.0.0.0 w kontenerze).

Dalsze kroki (poza tym plikiem): OpenTelemetry Collector → Prometheus/Grafana
albo export do Jaeger — wymaga osobnej konfiguracji eksporterów/pipeline'ów
Collector albo innego odbiornika, nie tylko „echo” tutaj.
"""
from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseServer
from typing import Any

from google.protobuf.json_format import Parse as json_parse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)


def _line(msg: str) -> None:
    print(msg, flush=True)


def _b64ish_hex(b: bytes) -> str:
    if not b:
        return ""
    return b.hex()


def _format_attrs(attrs) -> str:
    parts: list[str] = []
    for a in attrs:
        key = a.key
        v = a.value
        if v.HasField("string_value"):
            val = f'"{v.string_value}"'
        elif v.HasField("bool_value"):
            val = str(v.bool_value)
        elif v.HasField("int_value"):
            val = str(v.int_value)
        elif v.HasField("double_value"):
            val = str(v.double_value)
        elif v.HasField("array_value"):
            val = f"<array len={len(v.array_value.values)}>"
        else:
            val = "?"
        parts.append(f"{key}={val}")
    return ", ".join(parts)


def _print_export(req: ExportTraceServiceRequest) -> None:
    if not req.resource_spans:
        _line("  (brak resource_spans)")
        return
    for rs in req.resource_spans:
        res_attrs = _format_attrs(rs.resource.attributes) if rs.resource else ""
        if res_attrs:
            _line(f"  resource: {res_attrs}")
        for ils in rs.scope_spans:
            for sp in ils.spans:
                tid = _b64ish_hex(sp.trace_id)
                sid = _b64ish_hex(sp.span_id)
                ps = _b64ish_hex(sp.parent_span_id) if sp.parent_span_id else ""
                _line(
                    f"  span: name={sp.name!r}  trace_id={tid}  span_id={sid}"
                    f"  parent={ps or '(brak)'}  kind={sp.kind}"
                )
                if sp.attributes:
                    _line(f"    attributes: {_format_attrs(sp.attributes)}")
                for ev in sp.events:
                    _line(f"    event: {ev.name!r}  {_format_attrs(ev.attributes)}")
                if sp.status and sp.status.code:
                    d = sp.status.message or ""
                    _line(f"    status: code={sp.status.code}  message={d!r}")


def _parse_traces_body(
    data: bytes, content_type: str
) -> ExportTraceServiceRequest | None:
    ct = (content_type or "").lower().split(";")[0].strip()
    out = ExportTraceServiceRequest()
    if ct in ("application/json", "text/json", "json"):
        if not data:
            return out
        try:
            s = data.decode("utf-8", errors="replace")
            json_parse(s, out, ignore_unknown_fields=True)
            return out
        except Exception as e:  # noqa: BLE001 — debug receiver
            _line(f"  błąd parse JSON: {e}")
            return None
    if not data:
        return out
    try:
        out.ParseFromString(data)
        return out
    except Exception as e:  # noqa: BLE001
        _line(f"  błąd parse protobuf: {e}")
        return None


def _traces_handler_post(handler: BaseHTTPRequestHandler) -> None:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    body = handler.rfile.read(length) if length else b""
    ct = handler.headers.get("Content-Type", "")
    _line(f"--- /v1/traces  bytes={len(body)}  content-type={ct!r} ---")
    msg = _parse_traces_body(body, ct)
    if msg is not None:
        _print_export(msg)
    # Odpowiedź OTLP/HTTP: 200, ciało ExportTraceServiceResponse (może być puste)
    response = ExportTraceServiceResponse()
    b = response.SerializeToString()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-protobuf")
    handler.send_header("Content-Length", str(len(b)))
    handler.end_headers()
    if b:
        handler.wfile.write(b)


def make_handler() -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # krótsze logi; szczegóły ręcznie w _line
        def log_message(self, format: str, *args: Any) -> None:
            if os.environ.get("OTEL_RECEIVER_LOG_HTTPD", ""):
                super().log_message(format, *args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                msg = (
                    "OTLP/HTTP trace receiver. POST to /v1/traces. "
                    "Grafana/Prometheus/Jaeger: zbieraj przez OpenTelemetry Collector.\n"
                )
                b = msg.encode("utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/v1/traces" or self.path.rstrip("/") == "/v1/traces":
                _traces_handler_post(self)
                return
            # miejsce na później: /v1/metrics, /v1/logs
            self.send_error(404, "tego endpointa ten serwer nie obsługuje")

    return H


def main() -> int:
    host = os.environ.get("OTEL_RECEIVER_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("OTEL_RECEIVER_HTTP_PORT", "4318"))
    H = make_handler()
    server: BaseServer
    with ThreadingHTTPServer((host, port), H) as server:  # type: ignore[assignment]
        _line(
            f"OTLP/HTTP: {host}:{port} — wysyłaj trace’y: POST /v1/traces "
            f"(z kontenera na hosta: http://host.docker.internal:{port}/v1/traces)"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            _line("koniec (KeyboardInterrupt).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
