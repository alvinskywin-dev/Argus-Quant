FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl fonts-dejavu-core fonts-liberation tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY docker/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod +x /usr/local/bin/healthcheck.sh

RUN mkdir -p /app/logs /app/data

# Drop privileges
RUN useradd -r -u 10001 -m -d /home/bot bot && chown -R bot:bot /app
USER bot

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD /usr/local/bin/healthcheck.sh || exit 1

CMD ["python", "-m", "app.main"]
