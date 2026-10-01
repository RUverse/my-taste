FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MYTASTE_DATA_DIR=/data \
    MYTASTE_CACHE_DIR=/data/cache

# ffmpeg streams local libraries (remuxing, transcoding, subtitles).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system mytaste && adduser --system --ingroup mytaste mytaste

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir /data && chown mytaste:mytaste /data

USER mytaste
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

CMD ["mytaste", "serve", "--host", "0.0.0.0", "--port", "8000"]
