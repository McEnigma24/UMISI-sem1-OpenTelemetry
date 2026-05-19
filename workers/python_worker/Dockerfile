# Osobny obraz worker-python (bez wspólnej bazy z gateway-python).
# Kontekst buildu: katalog z ``docker-compose.yml`` (``docker compose build`` uruchamiaj stamtąd).
FROM python:3.10-slim

WORKDIR /app

COPY workers/python_worker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY workers/python_worker/_src /app/modes/complete
COPY workers/python_worker_incomplete/_src /app/modes/incomplete

COPY workers/python_worker/entrypoint_worker.py /app/entrypoint_worker.py

RUN python -c "import pathlib; c=pathlib.Path('/app/modes/complete'); ic=pathlib.Path('/app/modes/incomplete'); \
assert (c/'pipeline_server.py').is_file(), f'complete: {sorted(p.name for p in c.glob(\"*\"))}'; \
assert (c/'telemetry.py').is_file(); \
assert (ic/'telemetry.py').is_file(), f'incomplete: {sorted(p.name for p in ic.glob(\"*\"))}'"

ENV PYTHON_WORKER_MODE=complete

CMD ["/bin/sh", "-c", "sleep 2 && exec python /app/entrypoint_worker.py"]
