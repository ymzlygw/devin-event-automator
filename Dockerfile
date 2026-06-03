FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

# System deps required to download/extract the ngrok binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Pre-download the ngrok binary so the tunnel starts fast at runtime instead of
# fetching it on first use. pyngrok caches it under the install dir below.
ENV PYNGROK_INSTALL_DIR=/usr/local/bin
RUN python -c "from pyngrok import ngrok, conf; conf.get_default().ngrok_path='/usr/local/bin/ngrok'; ngrok.install_ngrok()" \
    && /usr/local/bin/ngrok --version

# Persisted JSON state (mounted as a volume in docker-compose).
RUN mkdir -p /data

COPY app.py .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
