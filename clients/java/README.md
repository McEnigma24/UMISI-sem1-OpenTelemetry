# Worker Java (pipeline HTTP)

Ten serwis zastępuje **domyślny** worker Node (`client-nodejs`, profil `nodejs`) w `docker-compose.yml`: ten sam kontrakt **POST `/v1/pipeline`**, domyślnie `DEMO_CLIENT_ID=ja` (krótki id jak w `stub/.../3-nested-route.json`), mapa `DEMO_PEER_*` (np. `DEMO_PEER_JA`), eksport trace’ów OTLP/HTTP do `otel:4318`.

**Metryki (jak Go/Python):** `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, interwał `OTEL_METRIC_EXPORT_INTERVAL` (ms, domyślnie 5000), observable gauges `demo.process.cpu.utilization` (%) i `demo.process.memory.usage` (heap+non-heap, By) z atrybutami `service.name` i `demo.client_id`. Wyłączenie: `DEMO_PROCESS_METRICS=false`.

**Pyroscope:** `PYROSCOPE_SERVER` (np. `http://pyroscope:4040`), `PYROSCOPE_ENABLED=false` żeby nie startować agenta (`io.pyroscope:agent`, JFR/itimer).

Kod Node pozostaje w `clients/nodejs/` — aby z powrotem uruchomić go w stacku, użyj profilu Dockera:

```bash
docker compose --profile nodejs up -d
```

Budowa samego obrazu z katalogu repo:

```bash
./clients/java/docker_build.sh
```
