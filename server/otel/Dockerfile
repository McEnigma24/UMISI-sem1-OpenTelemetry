# Cienka nakładka na oficjalny obraz: własna konfiguracja, ten sam interfejs co
# `otel/opentelemetry-collector-contrib` (Dockerfile nie przebudowuje collectora).
FROM otel/opentelemetry-collector-contrib:latest

COPY config.yaml /etc/otelcol/config.yaml

EXPOSE 4317
EXPOSE 4318

CMD ["--config=/etc/otelcol/config.yaml"]
