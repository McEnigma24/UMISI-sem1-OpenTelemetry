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

- **Extract** przy wejściu żądania: nagłówki → `opentelemetry.propagate.extract` → `otel_context.attach` w handlerze `do_POST` (`pipeline_server.py`).
- **Inject** przy forwardzie: `opentelemetry.propagate.inject(carrier)` wykonywane wewnątrz aktywnego spana `pipeline.forward` / `pipeline.nested_forward`, potem `urllib.request.Request(..., headers={**carrier, ...})`.
- **Gateway fix dla pierwszego hopa**: po `inject` gateway dodatkowo nadpisuje `carrier["traceparent"]` wartością z `SpanContext` aktualnego spana CLIENT i zapisuje ją jako atrybut `demo.forward.traceparent`.

### Dlaczego `demo.forward.traceparent` jest potrzebne

Po rozdzieleniu `gateway-python` i `worker-python` pojawił się objaw:

- trace wyszukany po `gateway_python` zawierał tylko spany gatewaya;
- `pipeline.forward` trwał tyle co cały downstream (np. ~4 s), więc HTTP forward faktycznie działał;
- trace wyszukany po `worker_rust` / innych workerach istniał osobno, ale miał inny `trace_id`.

To oznaczało, że problem nie był w routingu, OTLP ani Jaegerze, tylko w **pierwszym nagłówku W3C `traceparent` wysyłanym z gatewaya do kolejnego hopa**. Downstream dostawał request, ale nie dostawał poprawnego parenta dla trace'a gatewaya, więc zaczynał nowy trace.

Dlatego `pipeline_server._forward_to_next()` nie polega wyłącznie na globalnym propagatorze. Gateway buduje `traceparent` bezpośrednio z aktualnego spana CLIENT (`pipeline.forward` / `pipeline.nested_forward`) i nadpisuje nim carrier:

```text
00-<trace_id aktualnego pipeline.forward>-<span_id aktualnego pipeline.forward>-<flags>
```

Jeśli problem wróci, najpierw sprawdź w Jaegerze atrybut `demo.forward.traceparent` na spanie `gateway_python: pipeline.forward`. `trace_id` w tym atrybucie musi być taki sam jak trace ID downstream (`worker_rust`, `worker_csharp`, itd.). Jeśli downstream ma inny trace ID, pierwszy hop znowu nie używa `traceparent` z gatewaya.

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
- W `docker-compose.yml` są też aliasy z myślnikiem: `DEMO_PEER_PY-GATEWAY` i `DEMO_PEER_PY-WORKER`. Są celowe: część workerów historycznie mapowała suffix env `PY_WORKER` na klucz `py_worker`, a trasy używają `py-worker`. Bez aliasu nested hop `ja -> py-worker` kończył się 502 zanim `worker-python` dostał HTTP request.
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
