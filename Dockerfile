FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

COPY backend/ backend/
COPY frontend/ frontend/
COPY run.py .

RUN mkdir -p /data && \
    adduser --disabled-password --gecos "" --uid 1001 appuser && \
    chown -R appuser:appuser /app /data

USER appuser

CMD ["python", "run.py"]
