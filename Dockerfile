FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY run.py .

RUN mkdir -p /app/uploads /app/data

ENV PORT=8000

CMD ["sh", "-c", "python -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT"]
