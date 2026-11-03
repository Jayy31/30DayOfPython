# Engine image. Multi-arch base: builds natively on the Oracle Ampere (arm64) VM.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 engine

WORKDIR /app
COPY pyproject.toml README.md ./
COPY engine ./engine
RUN pip install ".[live]"
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini docker/entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /app/data && chown -R engine /app/data

USER engine
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1
ENTRYPOINT ["./entrypoint.sh"]
CMD ["run"]
