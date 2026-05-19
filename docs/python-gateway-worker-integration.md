# Integracja: `gateway-python` vs `worker-python` (Python)

Ten plik jest **checklistą** zmian i punktów integracji po rozdzieleniu na dwa obrazy Dockera (`gateway_python` i `worker_python`) oraz katalog `python_gateway/` w korzeniu repozytorium.

## Katalogi i obrazy

| Element | Ścieżka / tag |
|--------|----------------|
| Gateway (kod wejścia) | `python_gateway/main.py` |
| Dockerfile gateway | `python_gateway/Dockerfile` → obraz **`gateway_python`** |
| Źródła pipeline (współdzielone w sensie biznesowym) | `workers/python_worker/_src/` (`pipeline_server.py`, `telemetry.py`) |
| Dockerfile worker | `workers/python_worker/Dockerfile` → obraz **`worker_python`** |
| Punkt wejścia kontenera workera | `workers/python_worker/entrypoint_worker.py` |
| Tryb lab `incomplete` | `workers/python_worker_incomplete/_src/` kopiowane do `/app/modes/incomplete` w **obu** obrazach |
| Compose | `docker-compose.yml`: `build.context: .` dla obu serwisów; `DEMO_PYTHON_TIER` = `gateway` \| `worker` |

Nie ma już wspólnego obrazu pośredniego `python_worker_lib` — każdy obraz ma własny `FROM python:3.10-slim` i własny `pip install -r workers/python_worker/requirements.txt`.

## HTTP

- Serwer: `ThreadingHTTPServer` w `pipeline_server.py`, `POST` + ścieżka z `DEMO_HTTP_PATH` (domyślnie `/v1/pipeline`).
- Oba kontenery nasłuchują na `DEMO_HTTP_ADDR` (w compose: `0.0.0.0:8080`).

## Propagacja trace (W3C)

- **Extract** przy wejściu żądania: nagłówki → `_W3C_TRACE.extract` → `otel_context.attach` w handlerze `do_POST` (`pipeline_server.py`).
- **Inject / extract**: globalny W3C z `telemetry.init_telemetry()` (`set_global_textmap`) + `opentelemetry.propagate.inject` / `extract` w `pipeline_server.py` — jak w działającym `workers/python/_src/main.py`; forward: `inject(carrier)` wykonywane wewnątrz aktywnego spana `pipeline.forward` i `urllib.request.Request(..., headers={**carrier, ...})`.

## Spany (przykłady)

- `pipeline.hop` (SERVER), `pipeline.processing`, kroki CPU / zagnieżdżenia (`pipeline.nested_forward` jako CLIENT), nazwy z `activity` w JSON.

## Metryki

- Histogram `demo.pipeline.hop.duration_ms`, counter `demo.pipeline.messages`.
- Metryki procesu (CPU / pamięć) rejestrowane w `telemetry.init_telemetry` / `_register_process_metrics`; atrybuty punktów m.in. przez `process_metric_point_attributes()`.

## Logi

- `telemetry.log_pipeline_line` — stdout + opcjonalnie OTLP przez `LoggerProvider` / `LoggingHandler` po `init_telemetry` z `OTEL_DEMO_LOG_EXPORT`.

## Pyroscope

- Inicjalizacja push profilu w `telemetry.init_pyroscope_push` (zmienna `PYROSCOPE_SERVER` w compose).
- Korelacja trace ↔ profil: atrybuty spanów zgodnie z konwencją Pyroscope (np. `pyroscope.profile.id` tam gdzie jest ustawiane w kodzie referencyjnym).

## Zmienne środowiskowe (routing hopów)

- `DEMO_PEER_PY_GATEWAY`, `DEMO_PEER_PY_WORKER`, `DEMO_PEER_RS`, … — mapa URL-i dla `id` w JSON trasy (`py-gateway`, `py-worker`, itd.).
- `DEMO_WORKER_ID` — musi zgadzać się z pierwszym nieodwiedzonym segmentem `route[].id` dla danego węzła.
- `OTEL_SERVICE_NAME`, `OTEL_DEMO_RESOURCE_TAG` — identyfikacja usługi w backendach telemetrycznych.
- **`DEMO_PYTHON_TIER`**: `gateway` lub `worker` — trafia do resource attribute `demo.python.tier` (`telemetry._build_resource` w `complete` i `incomplete`).

## Skrypty

- `compose_build.sh` — `docker compose build --parallel` buduje m.in. oba obrazy Python.
- `compose_run.sh` — logi: `gateway-python`, `worker-python`, …
- `workers/python_worker/docker_build.sh` — opcjonalny ręczny build obu obrazów z korzenia repo.

## Healthchecki

- Zdefiniowane w `docker-compose.yml` dla `gateway-python` i `worker-python` (połączenie TCP na port 8080).

## Payloady demo (`stub/sender/python_traffic_generator/routes/`)

- Pierwszy hop wejściowy do API publicznego: **`py-gateway`** (nie używać samego `py` — walidacja w `pipeline_server.py`).

## Powiązany opis architektury HTTP

- Zob. [http-pipeline-architecture.md](http-pipeline-architecture.md).
