FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DENO_INSTALL=/usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY app ./app
COPY start.sh ./start.sh

RUN chmod +x ./start.sh \
    && python -m compileall -q app

ENV PORT=10000 \
    CORS_ORIGINS=* \
    VELO_MAX_CONCURRENT_JOBS=1 \
    VELO_MAX_QUEUE=2 \
    VELO_JOB_TTL_SECONDS=1800 \
    VELO_MAX_FILE_BYTES=1073741824 \
    VELO_INFO_LIMIT_PER_MINUTE=10 \
    VELO_DOWNLOAD_LIMIT_PER_HOUR=5

EXPOSE 10000

USER 65532:65532

ENTRYPOINT ["./start.sh"]
