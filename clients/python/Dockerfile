FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["/bin/sh", "-c", "sleep 2 && exec python _src/main.py"]

# CMD ["python", "_src/main.py"]
