# Production image for the read-only operational dashboard.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app
RUN groupadd --system quant && useradd --system --gid quant --create-home quant

COPY . .
RUN python -m pip install --upgrade pip && python -m pip install --no-cache-dir --no-deps .

RUN mkdir -p /app/var /app/logs && chown -R quant:quant /app
USER quant
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" || exit 1
ENTRYPOINT ["python", "-m", "dashboard.server"]
