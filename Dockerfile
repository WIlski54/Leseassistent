# LeseAssistent Dockerfile für Coolify
# Python 3.11 mit WebSocket-Support (gevent)

FROM python:3.11-slim

# System-Dependencies für PDF/DOCX Verarbeitung
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Arbeitsverzeichnis
WORKDIR /app

# Requirements zuerst (für Docker Cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code kopieren
COPY . .

# Port freigeben
EXPOSE 5000

# Umgebungsvariablen
ENV PYTHONUNBUFFERED=1
ENV ASYNC_MODE=gevent

# gunicorn mit gevent-websocket Worker für Socket.IO
CMD ["gunicorn", "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
